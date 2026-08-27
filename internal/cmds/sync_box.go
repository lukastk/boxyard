package cmds

import (
	"context"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/locking"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/ownership"
	"github.com/lukastk/boxyard/internal/remoteindex"
	"github.com/lukastk/boxyard/internal/syncengine"
	"github.com/lukastk/boxyard/internal/tombstones"
)

// SyncStore is everything SyncBox needs from storage. It is the union of the
// interfaces the domain packages already declare, so *storage.Adapter satisfies
// it without any new code.
type SyncStore interface {
	syncengine.Storage
	tombstones.Store
	ownership.Checker
	// ForRemoteIndex adapts the same store to remoteindex's listing type. The
	// two packages declare their own Entry, so the interfaces cannot simply be
	// embedded together.
	ForRemoteIndex() remoteindex.Store
}

// PermsWithSync is the exec-bit handling the sync engine needs, named here so
// the commands that merely pass it through do not have to import syncengine.
type PermsWithSync = syncengine.Perms

// PartResult pairs a part's decided status with whether a transfer happened.
type PartResult struct {
	Status syncengine.SyncStatus
	Synced bool
}

// SyncBoxOptions mirrors the Python `sync_box` signature.
type SyncBoxOptions struct {
	BoxIndexName string
	// Direction nil means "decide from the status" — only valid with CAREFUL.
	Direction *enums.SyncDirection
	Setting   enums.SyncSetting
	// Choices is the set of parts to REPORT on. Nil means all of them. Note
	// that META and CONF are SYNCED whenever DATA is regardless — see below.
	Choices            []enums.BoxPart
	Verbose            bool
	ShowRcloneProgress bool
	// TombstonedBoxIDs is every tombstoned box id at this box's storage
	// location, when the caller already holds the set. Nil falls back to a
	// single-box probe.
	TombstonedBoxIDs map[string]bool
	SkipLock         bool
	Out              io.Writer
}

// SyncBox syncs one box with its remote.
//
// Ported from pts/mod/cmds/03_sync_box.pct.py.
func SyncBox(ctx context.Context, cfg *config.Config, s SyncStore, p syncengine.Perms, opts SyncBoxOptions) (map[enums.BoxPart]PartResult, error) {
	choices := opts.Choices
	if choices == nil {
		choices = enums.AllBoxParts
	}
	wanted := map[enums.BoxPart]bool{}
	for _, part := range choices {
		wanted[part] = true
	}
	setting := opts.Setting
	if setting == "" {
		setting = enums.SyncCareful
	}

	printf := func(format string, a ...any) {
		if opts.Out != nil && opts.Verbose {
			fmt.Fprintf(opts.Out, format, a...)
		}
	}

	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return nil, err
	}
	bm, ok := meta.ByIndexName()[opts.BoxIndexName]
	if !ok {
		return nil, fmt.Errorf("Box '%s' not found.", opts.BoxIndexName)
	}

	slConfig, err := bm.StorageLocationConfig(cfg)
	if err != nil {
		return nil, err
	}
	storageLocation := bm.StorageLocation

	// A `local` storage location has no remote: its store is a directory on
	// this machine. That is a legitimate, permanent state — not an error, and
	// not something a retry resolves. Python returned nothing at all here,
	// while every caller reads a dict, so one such box turned into an
	// AttributeError rendered as a red multi-sync line every 1200s (v0.5.5).
	if slConfig.StorageType == config.StorageLocal {
		printf("Box '%s' is in the local storage location '%s'. Nothing to sync.\n",
			opts.BoxIndexName, storageLocation)
		return wholeBoxResult(choices, syncengine.LocalStorage, ""), nil
	}

	boxID, err := models.ExtractBoxID(opts.BoxIndexName)
	if err != nil {
		return nil, err
	}

	// One remote probe PER BOX is what TombstonedBoxIDs exists to avoid: a
	// multi-sync pass over 587 boxes made 587 separate SFTP connections to the
	// same storage box, every 20 minutes, on every machine. When the caller
	// does not hold the set — sync_box is also a standalone command — fall back
	// to the single probe rather than SKIPPING the check: a silent skip turns a
	// safety check into a no-op and lets a box deleted from another machine be
	// resurrected here.
	var isTombstoned bool
	if opts.TombstonedBoxIDs != nil {
		isTombstoned = opts.TombstonedBoxIDs[boxID]
	} else {
		if isTombstoned, err = tombstones.IsTombstoned(ctx, s, cfg, storageLocation, boxID); err != nil {
			return nil, err
		}
	}
	if isTombstoned {
		msg := fmt.Sprintf("Box '%s' was deleted", opts.BoxIndexName)
		if t, err := tombstones.Get(ctx, s, cfg, storageLocation, boxID); err == nil && t != nil {
			msg += fmt.Sprintf(" by %s at %s", t.DeletedByHostname, t.DeletedAtUTC)
		}
		// A warning, not a silence: a tombstoned box is skipped, and the
		// reason has to reach the operator even when Out is nil.
		fmt.Fprintf(os.Stderr, "Warning: %s. Skipping sync.\n", msg)
		return wholeBoxResult(choices, syncengine.Tombstoned, msg), nil
	}

	// Names may differ between local and remote, so the remote is addressed by
	// the index name the BOX ID resolves to there. A box that does not exist
	// remotely yet is new: use the local name.
	remoteIndexName, err := remoteindex.Find(ctx, s.ForRemoteIndex(), cfg, storageLocation, boxID)
	if err != nil {
		return nil, err
	}
	if remoteIndexName == "" {
		remoteIndexName = opts.BoxIndexName
	}

	if !opts.SkipLock {
		mgr := locking.NewManager(cfg.BoxyardDataPath)
		release, err := mgr.BoxSyncLock(opts.BoxIndexName, locking.BoxSyncLockTimeout)
		if err != nil {
			return nil, err
		}
		defer release()
	}

	printf("Syncing box %s at %s.\n", opts.BoxIndexName, storageLocation)

	remoteBackupsPath := path.Join(slConfig.StorePath, boxconst.RemoteBackupRelPath)
	results := map[enums.BoxPart]PartResult{}

	baseRequest := func(part enums.BoxPart) (syncengine.HelperRequest, error) {
		localPath, err := bm.LocalPartPath(cfg, part)
		if err != nil {
			return syncengine.HelperRequest{}, err
		}
		return syncengine.HelperRequest{
			StatusRequest: syncengine.StatusRequest{
				LocalPath:            localPath,
				LocalSyncRecordPath:  bm.LocalSyncRecordPath(cfg, part),
				Remote:               storageLocation,
				RemotePath:           remotePartPath(slConfig.StorePath, remoteIndexName, part),
				RemoteSyncRecordPath: remoteSyncRecordPath(slConfig.StorePath, remoteIndexName, part),
			},
			Direction:             opts.Direction,
			Setting:               setting,
			LocalSyncBackupsPath:  cfg.LocalSyncBackupsPath(),
			RemoteSyncBackupsPath: remoteBackupsPath,
			Verbose:               opts.Verbose,
			ShowRcloneProgress:    opts.ShowRcloneProgress,
		}, nil
	}

	// Python installs a "soft interruption" flag and checks it between parts,
	// so a Ctrl-C during a long multi-sync abandons the box at a part boundary
	// rather than mid-transfer. Go's equivalent is the context: a caller
	// cancels it, and this checks at the same three points Python does. Doing
	// it here rather than relying on the transfer to notice is the point —
	// the boundary is where abandoning a box is safe.
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	// --- META ---------------------------------------------------------
	//
	// Synced whenever DATA is, even when the caller did not ask for it: the
	// ownership decision below reads write_owner out of the boxmeta, and
	// `sync -c data` would otherwise decide it from a local copy that predates
	// another machine's claim. That is the one path by which a non-owner could
	// push without ever learning the box had been claimed.
	//
	// Its result is only RECORDED when META was requested, so the returned map
	// still answers exactly what the caller asked about.
	if wanted[enums.PartMeta] || wanted[enums.PartData] {
		printf("Syncing %s.\n", enums.PartMeta)
		req, err := baseRequest(enums.PartMeta)
		if err != nil {
			return nil, err
		}

		// If both sides have edited the boxmeta, try to reconcile them before
		// the sync sees it. Declines — no base, no remote copy, a scalar both
		// sides changed differently — leave the divergence exactly as it is,
		// so the refusal below is unchanged for every case this cannot settle.
		if err := tryMergeDivergedBoxmeta(ctx, cfg, s, bm, req, printf); err != nil {
			return nil, err
		}

		status, synced, err := syncengine.Run(ctx, s, p, req)
		if err != nil {
			return nil, err
		}
		if wanted[enums.PartMeta] {
			results[enums.PartMeta] = PartResult{Status: status, Synced: synced}
		}

		// Record the merge base, but ONLY where local and remote are known to
		// match: the status said SYNCED (nothing to do), or a transfer
		// completed and was verified. A base that never corresponded to a
		// shared state would make a later merge confidently wrong, which is
		// worse than the refusal a missing base falls back to.
		//
		// What this condition is worth TODAY: every non-agreeing META outcome
		// currently returns an ERROR from syncengine.Run, so control does not
		// reach here at all. It stays as defence in depth — WriteDenied is the
		// precedent for a refusal being turned into a returned status rather
		// than an error, to stop the supervisor logging the same thing 72
		// times a day — and if META ever gains such a status, this is what
		// keeps the base from moving under it.
		if status.Condition == syncengine.Synced || synced {
			if err := models.RecordMetaBase(cfg, bm); err != nil {
				return nil, err
			}
		}
	}

	// --- Write ownership ----------------------------------------------
	//
	// Decided HERE: after the META sync, so it reads whatever the rest of the
	// fleet last said about this box, and before CONF/DATA, which are the parts
	// ownership governs. Not inside syncengine — that takes paths and knows
	// nothing about boxes, and pushing box semantics into it would be the wrong
	// layer.
	//
	// `bm` came from the registry cache, read BEFORE the META sync, and is
	// stale the moment that sync pulls: re-read from disk.
	onDisk, err := models.LoadBoxMeta(cfg, storageLocation, opts.BoxIndexName)
	if err != nil {
		return nil, err
	}
	mayPush := ownership.MayPush(cfg, onDisk)

	// syncPart is syncengine.Run, except that a non-owner never pushes.
	//
	// A non-owner is a read-only replica doing its job, so it pulls quietly and
	// says nothing; it only speaks up when it holds changes that will never
	// leave this machine. Nothing here returns an error — see
	// syncengine.WriteDenied.
	syncPart := func(req syncengine.HelperRequest, probeInclude, probeExclude, probeFilters string) (syncengine.SyncStatus, bool, error) {
		if mayPush {
			return syncengine.Run(ctx, s, p, req)
		}

		deny := func(st syncengine.SyncStatus) (syncengine.SyncStatus, bool, error) {
			st.Condition = syncengine.WriteDenied
			st.ErrorMessage = ownership.WriteDeniedMessage(onDisk, "")
			return st, false, nil
		}

		// Only a NON-owner pays for this extra status read; an unowned box or
		// one we own takes the path above untouched.
		probe := req.StatusRequest
		probe.ExcludePath = probeExclude
		status, err := syncengine.GetSyncStatus(ctx, s, probe)
		if err != nil {
			return syncengine.SyncStatus{}, false, err
		}

		switch status.Condition {
		case syncengine.Synced:
			return status, false, nil

		case syncengine.NeedsPull:
			// The ordinary state of a read-only replica. Pull, silently.
			pull := enums.DirectionPull
			pullReq := req
			pullReq.Direction = &pull
			return syncengine.Run(ctx, s, p, pullReq)

		case syncengine.NeedsPush:
			// NeedsPush is not evidence of a real change: it comes from a tree
			// walk, so a single .DS_Store sets it even though the file can
			// never be transferred. Ask what a push would ACTUALLY move.
			wouldTransfer, err := ownership.PushWouldTransfer(ctx, s,
				req.LocalPath, req.Remote, req.RemotePath,
				probeInclude, probeExclude, probeFilters)
			if err != nil {
				// PushWouldTransfer already reports "would transfer" when it
				// could not answer, so the refusal below is the safe reading.
				return deny(status)
			}
			if !wouldTransfer {
				// Nothing to send. Report it as what it is rather than as a
				// refusal, or every machine would carry a permanent complaint
				// about changes that do not exist.
				status.Condition = syncengine.Synced
				return status, false, nil
			}
			return deny(status)

		case syncengine.Conflict:
			return deny(status)
		}

		// Error, Excluded, Tombstoned and the interrupted-sync conditions are
		// not ownership's business: they are handled exactly as before.
		return syncengine.Run(ctx, s, p, req)
	}

	if err := ctx.Err(); err != nil {
		return nil, err
	}

	// --- CONF ---------------------------------------------------------
	//
	// Synced whenever DATA is, for the same reason META is:
	// conf/.rclone_include|_exclude|_filters decide WHAT DATA syncs, and they
	// are read off the local disk immediately below. `sync -c data` on a
	// machine that has never pulled this box's conf/ would otherwise sync DATA
	// with the GLOBAL filters — so a box whose .rclone_include narrows what it
	// syncs would sync EVERYTHING (Python v0.5.5).
	confPath, err := bm.LocalPartPath(cfg, enums.PartConf)
	if err != nil {
		return nil, err
	}
	if wanted[enums.PartConf] || wanted[enums.PartData] {
		printf("Syncing %s\n", enums.PartConf)
		req, err := baseRequest(enums.PartConf)
		if err != nil {
			return nil, err
		}
		// CONF is optional — it may not exist on either side.
		req.AllowMissingSource = true
		// A missing local conf/ means "never fetched", NOT "deliberately
		// excluded" — exclusion is a DATA concept. Reading it as EXCLUDED made
		// the absence self-perpetuating, so a box's own rclone filters only
		// ever existed on the machine that wrote them (Python v0.5.3).
		req.TreatLocalAbsenceAsNeedsPull = true

		// CONF follows DATA's ownership rule: the filters decide what DATA
		// syncs, so letting a non-owner push CONF would let it change the
		// owner's filters. META, by contrast, stays writable by every machine
		// — without that, ownership could never be transferred and
		// groups/parents could not be edited from a non-owner.
		status, synced, err := syncPart(req, "", "", "")
		if err != nil {
			return nil, err
		}
		if wanted[enums.PartConf] {
			results[enums.PartConf] = PartResult{Status: status, Synced: synced}
		}
	}

	if err := ctx.Err(); err != nil {
		return nil, err
	}

	// --- DATA ---------------------------------------------------------
	//
	// The filter files are resolved from the NOW-SYNCED conf/.
	includePath := existingOrEmpty(filepath.Join(confPath, boxconst.RcloneIncludeFilename))
	excludePath := existingOrEmpty(filepath.Join(confPath, boxconst.RcloneExcludeFilename))
	if excludePath == "" {
		excludePath = cfg.DefaultRcloneExcludePath()
	}
	filtersPath := existingOrEmpty(filepath.Join(confPath, boxconst.RcloneFiltersFilename))

	if wanted[enums.PartData] {
		printf("Syncing %s\n", enums.PartData)
		req, err := baseRequest(enums.PartData)
		if err != nil {
			return nil, err
		}
		req.IncludePath = includePath
		req.ExcludePath = excludePath
		req.FiltersPath = filtersPath
		req.PreserveExecPerms = true
		// The status probe must see the same exclusions the transfer will, or
		// debris that can never move still marks the box as modified.
		req.StatusRequest.ExcludePath = excludePath

		status, synced, err := syncPart(req, includePath, excludePath, filtersPath)
		if err != nil {
			return nil, err
		}
		results[enums.PartData] = PartResult{Status: status, Synced: synced}
	}

	if err := remoteindex.Update(cfg, storageLocation, boxID, remoteIndexName); err != nil {
		return nil, err
	}
	if wanted[enums.PartMeta] {
		if _, err := models.RefreshBoxyardMeta(cfg, false); err != nil {
			return nil, err
		}
	}
	return results, nil
}

// wholeBoxResult builds the result for a condition that is a fact about the
// whole box rather than about any pair of paths.
func wholeBoxResult(choices []enums.BoxPart, condition syncengine.SyncCondition, message string) map[enums.BoxPart]PartResult {
	record := models.NewSyncRecord(false, "")
	out := make(map[enums.BoxPart]PartResult, len(choices))
	for _, part := range choices {
		out[part] = PartResult{Status: syncengine.SyncStatus{
			Condition:        condition,
			LocalPathExists:  condition == syncengine.LocalStorage,
			RemotePathExists: false,
			LocalSyncRecord:  &record,
			RemoteSyncRecord: &record,
			IsDir:            true,
			ErrorMessage:     message,
		}}
	}
	return out
}

func remotePartPath(storePath, indexName string, part enums.BoxPart) string {
	base := path.Join(storePath, boxconst.RemoteBoxesRelPath, indexName)
	switch part {
	case enums.PartData:
		return path.Join(base, boxconst.BoxDataRelPath)
	case enums.PartMeta:
		return path.Join(base, boxconst.BoxMetafileRelPath)
	default:
		return path.Join(base, boxconst.BoxConfRelPath)
	}
}

func remoteSyncRecordPath(storePath, indexName string, part enums.BoxPart) string {
	return path.Join(storePath, boxconst.SyncRecordsRelPath, indexName, string(part)+".rec")
}

// existingOrEmpty returns p if it exists, and "" otherwise. A filter file that
// is not there is a legitimate expected state — most boxes have none.
func existingOrEmpty(p string) string {
	if _, err := os.Stat(p); err == nil {
		return p
	}
	return ""
}

// errOutOr returns w when it is a writer we can warn through, and stderr
// otherwise. A warning that vanishes because the caller passed no writer is a
// silent failure.
func errOutOr(w io.Writer) io.Writer {
	if w != nil {
		return w
	}
	return os.Stderr
}

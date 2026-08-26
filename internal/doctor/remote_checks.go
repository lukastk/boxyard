package doctor

import (
	"context"
	"fmt"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/ownership"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/storage"
	"github.com/lukastk/boxyard/internal/syncengine"
	"github.com/lukastk/boxyard/internal/tombstones"
)

// RemoteStore is everything doctor's remote checks need. A nil store means the
// remote checks cannot run at all.
type RemoteStore interface {
	Lsjson(ctx context.Context, loc rclone.Location, o rclone.LsjsonOptions) ([]rclone.Entry, bool, error)
	Cat(ctx context.Context, remote, path string) (bool, string, error)
	ListJSON(ctx context.Context, remote, path string) ([]tombstones.Entry, error)
	Check(ctx context.Context, src, dst rclone.Location, o rclone.TransferOptions) (bool, []string, error)
	LocalLastModified(path string, excludeNames map[string]bool) (time.Time, bool, error)
}

// recordTimeSlack: a remote record written at our own record's moment IS our
// record, so only the rest need a round trip. rclone stamps the destination
// with the source temp file's mtime, which is the moment the ULID was minted,
// so for one and the same record the two agree closely — measured across 750
// records on this fleet the worst gap was 2.1s, and a 5s window costs zero
// extra fetches.
const recordTimeSlack = 5 * time.Second

// incompleteRemoteGrace: long enough that no real push on this fleet is still
// running (the largest box is ~100 GB and pushes in well under this), short
// enough that a genuine wedge is reported the same day rather than months
// later.
const incompleteRemoteGrace = 6 * time.Hour

func checkRemote(ctx context.Context, cfg *config.Config, s RemoteStore, report *Report, sc *scan, opts Options) error {
	remoteChecks := []string{"stale-meta-mirror", "tombstoned-box", "diverged-box"}
	if !opts.CheckRemote || !rcloneOK.available || s == nil {
		for _, name := range remoteChecks {
			report.Checks[name].Skipped = true
		}
		return nil
	}

	for _, slName := range sortedKeys(cfg.StorageLocations) {
		slConfig := cfg.StorageLocations[slName]
		if slConfig.StorageType != config.StorageRclone {
			continue
		}
		if len(opts.StorageLocations) > 0 && !containsStr(opts.StorageLocations, slName) {
			continue
		}
		checkStaleMetaMirror(ctx, cfg, s, report, sc, slName, slConfig.StorePath)
		checkTombstonedBoxes(ctx, cfg, s, report, sc, slName)
		checkDivergedBoxes(ctx, cfg, s, report, sc, slName, slConfig.StorePath)
	}
	return nil
}

func checkStaleMetaMirror(ctx context.Context, cfg *config.Config, s RemoteStore,
	report *Report, sc *scan, slName, storePath string) {

	entries, _, err := s.Lsjson(ctx, rclone.Remote(slName, path.Join(storePath, boxconst.RemoteBoxesRelPath)),
		rclone.LsjsonOptions{DirsOnly: true})
	if err != nil {
		report.add("stale-meta-mirror",
			fmt.Sprintf("Could not list remote storage location '%s'", slName),
			"Check connectivity and the rclone config, or run doctor with --no-remote to skip remote checks.",
			Field{"storage_location", slName})
		return
	}
	local := map[string]bool{}
	for _, name := range sc.registrationDirsBySL[slName] {
		local[name] = true
	}
	var missing []string
	for _, e := range entries {
		if e.IsDir && !local[e.Name] {
			missing = append(missing, e.Name)
		}
	}
	if len(missing) == 0 {
		return
	}
	sort.Strings(missing)
	report.add("stale-meta-mirror",
		fmt.Sprintf("%d remote box(es) on '%s' are not mirrored locally (newest: %s)",
			len(missing), slName, missing[len(missing)-1]),
		fmt.Sprintf("Run `boxyard sync-missing-meta -s %s` to fetch the missing boxmetas.", slName),
		Field{"storage_location", slName}, Field{"missing_index_names", missing})
}

func checkTombstonedBoxes(ctx context.Context, cfg *config.Config, s RemoteStore,
	report *Report, sc *scan, slName string) {

	ids, err := tombstones.ListBoxIDs(ctx, tombstoneStore{s}, cfg, slName)
	if err != nil {
		return
	}
	names := append([]string{}, sc.registrationDirsBySL[slName]...)
	sort.Strings(names)
	for _, regName := range names {
		boxID, _, err := models.ParseIndexName(regName)
		if err != nil || !ids[boxID] {
			continue
		}
		report.add("tombstoned-box",
			fmt.Sprintf("Box '%s' on '%s' is tombstoned on the remote (deleted from another machine) but still registered locally", regName, slName),
			fmt.Sprintf("Remove the local copy with `boxyard delete -r '%s'`, or remove the remote tombstone to resurrect the box.", regName),
			Field{"storage_location", slName}, Field{"index_name", regName}, Field{"box_id", boxID})
	}
}

// tombstoneStore adapts a RemoteStore to the narrower interface tombstones
// declares.
type tombstoneStore struct{ s RemoteStore }

func (t tombstoneStore) Write(context.Context, string, string, string) error { return nil }
func (t tombstoneStore) Cat(ctx context.Context, remote, p string) (bool, string, error) {
	return t.s.Cat(ctx, remote, p)
}
func (t tombstoneStore) PathExists(context.Context, string, string) (bool, bool, error) {
	return false, false, nil
}
func (t tombstoneStore) ListJSON(ctx context.Context, remote, p string) ([]tombstones.Entry, error) {
	return t.s.ListJSON(ctx, remote, p)
}
func (t tombstoneStore) Delete(context.Context, string, string) error { return nil }

func checkDivergedBoxes(ctx context.Context, cfg *config.Config, s RemoteStore,
	report *Report, sc *scan, slName, storePath string) {

	// ONE recursive listing of the sync records, not a round trip per box: the
	// per-box form is ~4 rclone calls x 3 parts x 583 boxes, which would make
	// doctor unusable.
	recordsRoot := path.Join(storePath, boxconst.SyncRecordsRelPath)
	entries, _, err := s.Lsjson(ctx, rclone.Remote(slName, recordsRoot),
		rclone.LsjsonOptions{FilesOnly: true, Recursive: true})
	if err != nil {
		report.add("diverged-box",
			fmt.Sprintf("Could not list the sync records on '%s', so no box on it could be checked for divergence: %v", slName, err),
			"Check connectivity and the rclone config, or pass --no-remote to skip the remote checks deliberately.",
			Field{"storage_location", slName})
		return
	}
	modTimes := map[string]time.Time{}
	for _, e := range entries {
		if t, err := time.Parse(time.RFC3339, trimNanos(e.ModTime)); err == nil {
			modTimes[e.Path] = t
		}
	}

	now := time.Now().UTC()
	for _, bm := range sc.boxMetas {
		if bm.StorageLocation != slName {
			continue
		}
		for _, part := range enums.AllBoxParts {
			localRec := readLocalRecord(bm.LocalSyncRecordPath(cfg, part))
			if localRec == nil {
				continue
			}
			localTime, err := localRec.Time()
			if err != nil {
				continue
			}
			relPath := path.Join(bm.IndexName(), string(part)+".rec")
			remoteMod, ok := modTimes[relPath]
			if !ok {
				continue
			}
			// A remote record written at our own record's moment IS ours.
			if absDuration(remoteMod.Sub(localTime)) <= recordTimeSlack {
				continue
			}
			remoteRec := readRemoteRecord(ctx, s, slName, path.Join(recordsRoot, relPath))
			if remoteRec == nil {
				continue
			}
			remoteTime, err := remoteRec.Time()
			if err != nil {
				continue
			}
			if remoteRec.ULID == localRec.ULID {
				continue
			}

			if !remoteRec.SyncComplete {
				age := now.Sub(remoteTime)
				if age < incompleteRemoteGrace {
					// Probably a push still in flight.
					continue
				}
				report.add("diverged-box",
					fmt.Sprintf("A push of the %s of '%s' from '%s' never completed (%s, %dd ago), so the remote copy may be half-written",
						part, bm.IndexName(), remoteRec.SyncerHostname,
						remoteTime.Format("2006-01-02"), int(age.Hours()/24)),
					fmt.Sprintf("Syncing here will refuse until it is resolved. Check whether '%s' still has the box, "+
						"then re-run the push from whichever machine holds the good copy: "+
						"`boxyard sync -r '%s' --sync-direction push --sync-setting force --sync-choices %s`.",
						remoteRec.SyncerHostname, bm.IndexName(), part),
					Field{"index_name", bm.IndexName()}, Field{"box_part", string(part)},
					Field{"storage_location", slName})
				continue
			}

			// Both records complete but different: a plain needs_pull is
			// harmless and the next sync fixes it. Only a LOCAL change on top
			// makes it a real divergence.
			localChanged := localTreeChanged(cfg, s, bm, part, localTime)
			if !localChanged {
				continue
			}
			suffix := " and the local copy has changed since"
			report.add("diverged-box",
				fmt.Sprintf("The %s of '%s' has diverged: local record %s from '%s', remote record %s from '%s'%s",
					part, bm.IndexName(),
					localTime.Format("2006-01-02"), localRec.SyncerHostname,
					remoteTime.Format("2006-01-02"), remoteRec.SyncerHostname, suffix),
				fmt.Sprintf("Both sides moved on independently, so sync refuses rather than pick a winner. "+
					"Look before choosing: `boxyard box-status -r '%s'`, and compare against the remote.",
					bm.IndexName()),
				Field{"index_name", bm.IndexName()}, Field{"box_part", string(part)},
				Field{"storage_location", slName})
		}
	}
}

func localTreeChanged(cfg *config.Config, s RemoteStore, bm *models.BoxMeta,
	part enums.BoxPart, recordTime time.Time) bool {

	localPath, err := bm.LocalPartPath(cfg, part)
	if err != nil {
		return false
	}
	excludePath := effectiveExclude(cfg, bm)
	modified, found, err := s.LocalLastModified(localPath, syncengine.LiteralExcludeNames(excludePath))
	if err != nil || !found {
		return false
	}
	return modified.After(recordTime)
}

func effectiveExclude(cfg *config.Config, bm *models.BoxMeta) string {
	confPath, err := bm.LocalPartPath(cfg, enums.PartConf)
	if err != nil {
		return cfg.DefaultRcloneExcludePath()
	}
	boxExclude := filepath.Join(confPath, boxconst.RcloneExcludeFilename)
	if fileExists(boxExclude) {
		return boxExclude
	}
	return cfg.DefaultRcloneExcludePath()
}

func checkOwnership(ctx context.Context, cfg *config.Config, s RemoteStore, report *Report, sc *scan) error {
	ownerCounts := map[string]int{}
	for _, bm := range sc.boxMetas {
		if bm.WriteOwner != "" {
			ownerCounts[bm.WriteOwner]++
		}
	}
	// Until some machine owns more than one box, "owns exactly one box" says
	// nothing.
	established := false
	for _, n := range ownerCounts {
		if n > 1 {
			established = true
		}
	}

	for _, bm := range sc.boxMetas {
		if bm.WriteOwner == "" {
			continue
		}
		if bm.WriteOwner == cfg.MachineName {
			if !bm.CheckIncluded(cfg) {
				report.add("stale-owner",
					fmt.Sprintf("Box '%s' names this machine ('%s') as its write owner, but it is not included here — so the one machine allowed to push it does not have it",
						bm.IndexName(), cfg.MachineName),
					fmt.Sprintf("No machine can push this box until that is fixed. Either give it up with "+
						"`boxyard release -r '%s'`, or take the box back with `boxyard include -r '%s'`.",
						bm.IndexName(), bm.IndexName()),
					Field{"index_name", bm.IndexName()}, Field{"write_owner", bm.WriteOwner},
					Field{"storage_location", bm.StorageLocation})
			}
			continue
		}
		if established && ownerCounts[bm.WriteOwner] == 1 {
			report.add("stale-owner",
				fmt.Sprintf("Box '%s' is owned by '%s', which owns no other box in this yard",
					bm.IndexName(), bm.WriteOwner),
				fmt.Sprintf("Probably a machine that was renamed or retired, in which case no machine can push "+
					"this box. If '%s' is real and simply owns only this box, nothing is wrong. Otherwise take "+
					"it over from the machine that should have it: `boxyard claim --steal -r '%s'`.",
					bm.WriteOwner, bm.IndexName()),
				Field{"index_name", bm.IndexName()}, Field{"write_owner", bm.WriteOwner},
				Field{"storage_location", bm.StorageLocation})
		}
	}
	return nil
}

// checkWriteDenied reports a box owned by another machine that has local
// changes which will never be pushed.
//
// This is the ONLY report of that state: sync stays deliberately silent about
// it, so if doctor did not say it, nothing would.
func checkWriteDenied(ctx context.Context, cfg *config.Config, s RemoteStore,
	report *Report, sc *scan, checkRemote bool) {

	if !checkRemote || !rcloneOK.available || s == nil {
		report.Checks["write-denied"].Skipped = true
		return
	}
	for _, bm := range sc.boxMetas {
		if bm.WriteOwner == "" || bm.WriteOwner == cfg.MachineName {
			continue
		}
		if !bm.CheckIncluded(cfg) {
			continue
		}
		slConfig, err := bm.StorageLocationConfig(cfg)
		if err != nil || slConfig.StorageType != config.StorageRclone {
			continue
		}
		dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
		if err != nil {
			continue
		}
		rec := readLocalRecord(bm.LocalSyncRecordPath(cfg, enums.PartData))
		if rec == nil {
			// Never synced here — that is interrupted-sync / stale-cache
			// territory, not this check's.
			continue
		}
		recTime, err := rec.Time()
		if err != nil {
			continue
		}
		excludePath := effectiveExclude(cfg, bm)
		modified, found, err := s.LocalLastModified(dataPath, syncengine.LiteralExcludeNames(excludePath))
		if err != nil || !found || !modified.After(recTime) {
			continue
		}

		// Only NOW is a remote call worth making. needs_push is not evidence of
		// a real change — a single .DS_Store sets it — so ask what a push would
		// actually move, under the box's real filters.
		confPath, _ := bm.LocalPartPath(cfg, enums.PartConf)
		includePath := optionalFile(filepath.Join(confPath, boxconst.RcloneIncludeFilename))
		filtersPath := optionalFile(filepath.Join(confPath, boxconst.RcloneFiltersFilename))
		remoteData := path.Join(slConfig.StorePath, boxconst.RemoteBoxesRelPath,
			bm.IndexName(), boxconst.BoxDataRelPath)

		wouldTransfer, err := ownership.PushWouldTransfer(ctx, s,
			dataPath, bm.StorageLocation, remoteData, includePath, excludePath, filtersPath)
		if err != nil || !wouldTransfer {
			continue
		}
		report.add("write-denied",
			ownership.WriteDeniedMessage(bm, "")+" This copy has local changes that will never leave this machine.",
			ownership.WriteDeniedHint(cfg, bm),
			Field{"index_name", bm.IndexName()}, Field{"write_owner", bm.WriteOwner},
			Field{"storage_location", bm.StorageLocation})
	}
}

func containsStr(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

func absDuration(d time.Duration) time.Duration {
	if d < 0 {
		return -d
	}
	return d
}

// trimNanos cuts rclone's nanosecond precision back to what time.Parse accepts
// alongside a zone offset.
func trimNanos(raw string) string {
	if i := strings.IndexByte(raw, '.'); i >= 0 {
		j := i + 1
		for j < len(raw) && raw[j] >= '0' && raw[j] <= '9' {
			j++
		}
		frac := raw[i+1 : j]
		if len(frac) > 9 {
			frac = frac[:9]
		}
		return raw[:i+1] + frac + raw[j:]
	}
	return raw
}

var _ = storage.New

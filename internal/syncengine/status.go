// Package syncengine holds boxyard's sync decision logic: the state machine
// that decides what, if anything, is safe to do with a box part, and the
// helper that carries it out.
//
// Ported from `get_sync_status` in src/boxyard/_models.py and `sync_helper` in
// src/boxyard/_utils/sync_helper.py.
//
// The state machine is deliberately expressed against the Prober interface
// rather than calling rclone directly. That is not indirection for its own
// sake: it is the riskiest logic in boxyard — a wrong answer here overwrites
// or loses a box — and an interface lets every branch be tested exhaustively
// and deterministically, with no remote and no filesystem.
package syncengine

import (
	"context"
	"fmt"
	"time"

	"github.com/lukastk/boxyard/internal/models"
)

// SyncCondition is the state machine's verdict for one box part.
type SyncCondition string

const (
	// Synced: the two sides agree, and nothing local has changed since.
	Synced SyncCondition = "synced"
	// SyncToRemoteIncomplete: a push was interrupted; the REMOTE may be corrupt.
	SyncToRemoteIncomplete SyncCondition = "sync_to_remote_incomplete"
	// SyncFromRemoteIncomplete: a pull was interrupted; the LOCAL side may be corrupt.
	SyncFromRemoteIncomplete SyncCondition = "sync_from_remote_incomplete"
	// Conflict: both sides moved on independently.
	Conflict  SyncCondition = "conflict"
	NeedsPush SyncCondition = "needs_push"
	NeedsPull SyncCondition = "needs_pull"
	// Excluded: the box exists remotely but is not checked out here.
	Excluded SyncCondition = "excluded"
	// Error: an inconsistent state that must not be acted on automatically.
	Error SyncCondition = "error"
	// Tombstoned: the box was deleted on the remote.
	Tombstoned SyncCondition = "tombstoned"
	// LocalStorage: the box's storage location is `local`, so there is no
	// remote to compare against — the store is a directory on this machine.
	//
	// Like Tombstoned, this is a fact about the BOX, not about a pair of
	// paths, so GetSyncStatus never produces it; the commands substitute it
	// for the whole box. Python learned in v0.5.5 that the alternative — an
	// early return with no result — makes `multi-sync` raise on every pass.
	LocalStorage SyncCondition = "local_storage"
)

// SyncStatus is the full result of probing one box part.
type SyncStatus struct {
	Condition        SyncCondition
	LocalPathExists  bool
	RemotePathExists bool
	LocalSyncRecord  *models.SyncRecord
	RemoteSyncRecord *models.SyncRecord
	IsDir            bool
	ErrorMessage     string
}

// Prober is the storage access the state machine needs.
//
// Note that the Python probes the LOCAL path through rclone too, not through
// os.Stat — so that local and remote are described by exactly the same code
// path. An implementation should preserve that.
type Prober interface {
	// PathExists reports whether a path exists and whether it is a directory.
	// An empty remote means the local filesystem.
	PathExists(ctx context.Context, remote, path string) (exists, isDir bool, err error)

	// ReadSyncRecord returns the record at path, or nil if there is none.
	// A missing record is a legitimate expected state, not an error.
	ReadSyncRecord(ctx context.Context, remote, path string) (*models.SyncRecord, error)

	// LocalIsEmptyDir reports whether a local directory has no entries.
	LocalIsEmptyDir(path string) (bool, error)

	// LocalLastModified returns the most recent modification time across every
	// regular file beneath path, and whether any was found at all.
	//
	// excludeNames are literal file/directory names to SKIP. Skipping them is
	// not an optimisation: this answer drives the sync decision, and without it
	// a file that can never be transferred still marks the box as modified.
	// macOS Finder writing a `.DS_Store` was enough to flip a real box to
	// NEEDS_PUSH and -- when the remote had also moved on -- to CONFLICT. That
	// is the mechanism behind boxes wedging from OS debris alone.
	LocalLastModified(path string, excludeNames map[string]bool) (t time.Time, found bool, err error)
}

// StatusRequest names the four paths one part's status depends on.
type StatusRequest struct {
	// TreatLocalAbsenceAsNeedsPull says how to read "absent locally, present
	// remotely".
	//
	// For DATA that means the box is not INCLUDED here — a deliberate choice
	// that must stay Excluded, since pulling it would undo a `boxyard exclude`.
	//
	// For CONF it means the opposite: nobody chose anything, the files have
	// simply never been fetched. Reading that as Excluded makes the absence
	// SELF-PERPETUATING — conf/ is missing, so it is judged excluded, so it is
	// never pulled, so it stays missing — and the effect is that a box's own
	// rclone filters exist only on the machine that wrote them. A box whose
	// conf/.rclone_include narrows what it syncs would sync EVERYTHING on the
	// second machine. Fixed in Python v0.5.3; this is the same branch.
	//
	// The field is phrased so the ZERO VALUE is DATA's meaning — the safe one.
	// Python defaults its equivalent to True (excluded) and Go cannot default a
	// bool to true, so the flag is inverted rather than renamed directly: a
	// caller that forgets to set it gets today's behaviour, not a silent
	// un-exclusion that pulls a removed box back onto the machine.
	TreatLocalAbsenceAsNeedsPull bool

	// ExcludePath is the box part's EFFECTIVE rclone exclude file -- its own
	// conf/.rclone_exclude if it has one, else the global default. It must be
	// the box's OWN effective file, never a hardcoded default: a per-box
	// exclude file REPLACES the global one, so assuming the defaults for a box
	// that overrides them could prune a directory the box really does sync,
	// hiding genuine changes. Empty means no exclusions.
	ExcludePath string

	LocalPath            string
	LocalSyncRecordPath  string
	Remote               string
	RemotePath           string
	RemoteSyncRecordPath string
}

// GetSyncStatus decides the state of one box part.
//
// The structure below deliberately mirrors the Python branch for branch. It is
// tempting to simplify — several branches look redundant — but each encodes a
// distinct real-world failure, and the tests pin all of them.
func GetSyncStatus(ctx context.Context, p Prober, req StatusRequest) (SyncStatus, error) {
	localExists, localIsDir, err := p.PathExists(ctx, "", req.LocalPath)
	if err != nil {
		return SyncStatus{}, err
	}

	// Default to "empty" when the path is absent or is not a directory.
	localIsEmpty := true
	if localExists && localIsDir {
		localIsEmpty, err = p.LocalIsEmptyDir(req.LocalPath)
		if err != nil {
			return SyncStatus{}, err
		}
	}

	remoteExists, remoteIsDir, err := p.PathExists(ctx, req.Remote, req.RemotePath)
	if err != nil {
		return SyncStatus{}, err
	}

	// A file on one side and a directory on the other is not something any
	// sync direction can reconcile.
	if localExists && remoteExists && localIsDir != remoteIsDir {
		return SyncStatus{}, fmt.Errorf(
			"Local and remote paths are not both files or both directories. Local is %s and remote is %s. Local path: '%s', remote path: '%s'.",
			dirWord(localIsDir), dirWord(remoteIsDir), req.LocalPath, req.RemotePath)
	}

	status := SyncStatus{
		LocalPathExists:  localExists,
		RemotePathExists: remoteExists,
		IsDir:            localIsDir || remoteIsDir,
	}

	if status.LocalSyncRecord, err = p.ReadSyncRecord(ctx, "", req.LocalSyncRecordPath); err != nil {
		return SyncStatus{}, err
	}
	if status.RemoteSyncRecord, err = p.ReadSyncRecord(ctx, req.Remote, req.RemoteSyncRecordPath); err != nil {
		return SyncStatus{}, err
	}
	local, remote := status.LocalSyncRecord, status.RemoteSyncRecord

	// Remote content with no record means we cannot reason about it at all.
	if remoteExists && remote == nil {
		status.Condition = Error
		status.ErrorMessage = fmt.Sprintf(
			"Something wrong here. Remote path exists, but remote sync record does not exist. Local path: '%s', remote path: '%s.",
			req.LocalPath, req.RemotePath)
		return status, nil
	}

	lastModified, foundModified, err := p.LocalLastModified(
		req.LocalPath, LiteralExcludeNames(req.ExcludePath))
	if err != nil {
		return SyncStatus{}, err
	}
	if !foundModified && localExists {
		// A file, or a non-empty directory, must have a modification time. Not
		// having one means something is wrong with the local path.
		if !localIsDir || (localIsDir && !localIsEmpty) {
			status.Condition = Error
			status.ErrorMessage = fmt.Sprintf(
				"Something wrong here. Local path exists and is not empty, but cannot be checked for last modification. Local path: '%s', remote path: '%s.",
				req.LocalPath, req.RemotePath)
			return status, nil
		}
	}

	localIncomplete := local != nil && !local.SyncComplete
	remoteIncomplete := remote != nil && !remote.SyncComplete

	switch {
	case localIncomplete && remoteIncomplete:
		if local.ULID == remote.ULID {
			// Same sync session marked both sides: an interrupted PUSH from
			// THIS machine, since push writes the incomplete record to both.
			status.Condition = SyncToRemoteIncomplete
		} else {
			status.Condition = Error
			status.ErrorMessage = fmt.Sprintf(
				"Inconsistent incomplete records (different ULIDs). Local ULID: %s, Remote ULID: %s",
				local.ULID, remote.ULID)
			return status, nil
		}

	case remoteIncomplete:
		// Only the remote is incomplete: a push was interrupted, possibly from
		// another machine.
		status.Condition = SyncToRemoteIncomplete

	case localIncomplete:
		// Only the local side is incomplete: a pull was interrupted here.
		status.Condition = SyncFromRemoteIncomplete

	default:
		recordsMatch := local != nil && remote != nil && local.ULID == remote.ULID
		switch {
		case recordsMatch:
			if foundModified && newerThanRecord(lastModified, local) {
				status.Condition = NeedsPush
			} else {
				status.Condition = Synced
			}

		case localExists && remoteExists:
			if local == nil {
				status.Condition = Error
				status.ErrorMessage = fmt.Sprintf(
					"Something wrong here. Local sync record does not exist, but the local and remote path exists. Local path: '%s', remote path: '%s.",
					req.LocalPath, req.RemotePath)
				return status, nil
			}
			remoteNewer, err := remoteSyncMoreRecent(remote, local)
			if err != nil {
				return SyncStatus{}, err
			}
			if remoteNewer {
				if foundModified && newerThanRecord(lastModified, local) {
					// The remote moved on AND we have local changes.
					status.Condition = Conflict
				} else {
					status.Condition = NeedsPull
				}
			} else {
				// Records differ but ours is not older — the two sides
				// diverged.
				status.Condition = Conflict
			}

		case localExists:
			if local != nil {
				status.Condition = Error
				status.ErrorMessage = fmt.Sprintf(
					"Something wrong here. Local sync record exists, but remote path does not exist. Local path: '%s', remote path: '%s.",
					req.LocalPath, req.RemotePath)
				return status, nil
			}
			status.Condition = NeedsPush

		case remoteExists:
			if req.TreatLocalAbsenceAsNeedsPull {
				status.Condition = NeedsPull
			} else {
				status.Condition = Excluded
			}

		default:
			// Neither side exists. Synced by definition — commonly the case
			// for `conf`, which many boxes never have.
			status.Condition = Synced
		}
	}

	return status, nil
}

func dirWord(isDir bool) string {
	if isDir {
		return "directory"
	}
	return "file"
}

// newerThanRecord reports whether the local tree changed after the recorded
// sync. Python compares the mtime against the RECORD'S timestamp, which is
// derived from its ULID.
func newerThanRecord(lastModified time.Time, record *models.SyncRecord) bool {
	if record == nil {
		return false
	}
	t, err := record.Time()
	if err != nil {
		return false
	}
	return lastModified.After(t)
}

func remoteSyncMoreRecent(remote, local *models.SyncRecord) (bool, error) {
	rt, err := remote.Time()
	if err != nil {
		return false, err
	}
	lt, err := local.Time()
	if err != nil {
		return false, err
	}
	return rt.After(lt), nil
}

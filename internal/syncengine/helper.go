package syncengine

import (
	"context"
	"fmt"
	"path"
	"path/filepath"
	"strings"

	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
)

// SyncFailedError means rclone reported failure. The incomplete sync records
// are deliberately left in place so the next run can see what happened.
type SyncFailedError struct {
	Stdout string
	Stderr string
}

func (e *SyncFailedError) Error() string {
	return fmt.Sprintf("Sync failed. Rclone output:\n%s\n%s", e.Stdout, e.Stderr)
}

// SyncUnsafeError means the sync was refused because it could lose data.
type SyncUnsafeError struct{ Message string }

func (e *SyncUnsafeError) Error() string { return e.Message }

// InvalidRemotePathError guards the safety mechanisms, which rely on the
// remote path being non-empty.
type InvalidRemotePathError struct{ Message string }

func (e *InvalidRemotePathError) Error() string { return e.Message }

// SyncOptions describes one rclone sync invocation.
type SyncOptions struct {
	Source, SourcePath string
	Dest, DestPath     string
	BackupPath         string
	Include, Exclude   []string
	Filter             []string
	IncludeFile        string
	ExcludeFile        string
	FiltersFile        string
	ShowProgress       bool
}

// Storage is everything sync_helper needs from the outside world. Keeping it
// an interface means the whole decision-and-ordering logic below — which is
// what protects a box from being overwritten — can be tested without a remote.
type Storage interface {
	Prober
	Mkdir(ctx context.Context, remote, path string) error
	Sync(ctx context.Context, opts SyncOptions) (ok bool, stdout, stderr string, err error)
	Purge(ctx context.Context, remote, path string) error
	WriteSyncRecord(ctx context.Context, remote, path string, rec models.SyncRecord) error
}

// Perms is the exec-bit manifest handling, split out so a sync can be tested
// without touching file modes.
type Perms interface {
	Generate(root string) (bool, error)
	Apply(root string) ([]string, error)
}

// HelperRequest is one part's sync.
type HelperRequest struct {
	StatusRequest

	Direction *enums.SyncDirection // nil means auto
	Setting   enums.SyncSetting

	LocalSyncBackupsPath  string
	RemoteSyncBackupsPath string

	IncludePath string
	ExcludePath string
	FiltersPath string
	Include     []string
	Exclude     []string
	Filter      []string

	DeleteBackup       bool
	SyncerHostname     string
	Verbose            bool
	ShowRcloneProgress bool
	AllowMissingSource bool
	PreserveExecPerms  bool
}

// canSafelyRetryIncomplete reports whether THIS machine may retry an
// interrupted sync.
//
// For an interrupted PULL the local side is incomplete and this machine owns
// it, so retrying is safe. For an interrupted PUSH the remote is incomplete,
// and retrying is only safe if this machine started it — proven by both sides
// carrying the SAME incomplete ULID, which only a push from here produces.
func canSafelyRetryIncomplete(cond SyncCondition, dir enums.SyncDirection, local, remote *models.SyncRecord) bool {
	switch cond {
	case SyncFromRemoteIncomplete:
		return dir == enums.DirectionPull
	case SyncToRemoteIncomplete:
		if local != nil && remote != nil &&
			!local.SyncComplete && !remote.SyncComplete &&
			local.ULID == remote.ULID {
			return dir == enums.DirectionPush
		}
	}
	return false
}

func unsafe(st SyncStatus, req HelperRequest, message string) error {
	if message != "" {
		return &SyncUnsafeError{Message: message}
	}
	return &SyncUnsafeError{Message: strings.TrimSpace(fmt.Sprintf(`Sync is unsafe. Info:
    Local exists: %v
    Remote exists: %v
    Local sync record: %v
    Remote sync record: %v
    Sync condition: %s`,
		st.LocalPathExists, st.RemotePathExists, st.LocalSyncRecord, st.RemoteSyncRecord, st.Condition))}
}

// Run performs the standard routine for syncing one local/remote pair.
//
// It returns the status it decided on and whether a transfer actually happened.
func Run(ctx context.Context, s Storage, p Perms, req HelperRequest) (SyncStatus, bool, error) {
	// An empty remote path would let the safety mechanisms address the whole
	// store, so it is disqualified outright.
	if req.RemotePath == "" {
		return SyncStatus{}, false, &InvalidRemotePathError{Message: "Remote path cannot be empty."}
	}
	if req.Direction == nil && req.Setting != enums.SyncCareful {
		return SyncStatus{}, false, fmt.Errorf("Auto sync direction can only be used with careful sync setting.")
	}

	status, err := GetSyncStatus(ctx, s, req.StatusRequest)
	if err != nil {
		return SyncStatus{}, false, err
	}
	if status.Condition == Error && req.Setting != enums.SyncForce {
		return status, false, fmt.Errorf("%s", status.ErrorMessage)
	}

	if req.Setting != enums.SyncForce && status.Condition == Synced {
		if req.Verbose {
			fmt.Println("Sync not needed.")
		}
		return status, false, nil
	}

	direction, done, err := resolveDirection(status, req)
	if err != nil || done {
		return status, false, err
	}

	if err := checkCarefulIsSafe(status, req, direction); err != nil {
		return status, false, err
	}

	if req.AllowMissingSource {
		sourceExists := status.LocalPathExists
		if direction == enums.DirectionPull {
			sourceExists = status.RemotePathExists
		}
		if !sourceExists {
			if req.Verbose {
				fmt.Println("Source does not exist and allow_missing_source=True. Skipping sync.")
			}
			return status, false, nil
		}
	}

	return performSync(ctx, s, p, req, status, direction)
}

// resolveDirection picks a direction when the caller did not. The second
// return reports "nothing to do".
func resolveDirection(status SyncStatus, req HelperRequest) (enums.SyncDirection, bool, error) {
	if req.Direction != nil {
		return *req.Direction, false, nil
	}
	switch status.Condition {
	case NeedsPush:
		return enums.DirectionPush, false, nil
	case NeedsPull:
		return enums.DirectionPull, false, nil
	case Excluded:
		if req.Verbose {
			fmt.Println("Sync not needed as the box is excluded.")
		}
		return "", true, nil
	case SyncFromRemoteIncomplete:
		if canSafelyRetryIncomplete(status.Condition, enums.DirectionPull, status.LocalSyncRecord, status.RemoteSyncRecord) {
			return enums.DirectionPull, false, nil
		}
		return "", false, unsafe(status, req, "")
	case SyncToRemoteIncomplete:
		if canSafelyRetryIncomplete(status.Condition, enums.DirectionPush, status.LocalSyncRecord, status.RemoteSyncRecord) {
			return enums.DirectionPush, false, nil
		}
		return "", false, unsafe(status, req,
			"Remote has an incomplete sync from another machine. "+
				"Use --sync-setting force to override, or sync from the original machine.")
	default:
		// SYNCED is unreachable here: auto implies CAREFUL, and CAREFUL returns
		// early on SYNCED above.
		return "", false, unsafe(status, req, "")
	}
}

// checkCarefulIsSafe applies the CAREFUL gate. REPLACE and FORCE deliberately
// skip it.
func checkCarefulIsSafe(status SyncStatus, req HelperRequest, direction enums.SyncDirection) error {
	if req.Setting != enums.SyncCareful {
		return nil
	}
	switch direction {
	case enums.DirectionPush:
		switch status.Condition {
		case NeedsPush, Synced:
			return nil
		case SyncToRemoteIncomplete:
			if !canSafelyRetryIncomplete(status.Condition, direction, status.LocalSyncRecord, status.RemoteSyncRecord) {
				return unsafe(status, req,
					"Remote has an incomplete sync from another machine. "+
						"Use --sync-setting force to override, or sync from the original machine.")
			}
			return nil
		default:
			return unsafe(status, req, "")
		}
	case enums.DirectionPull:
		switch status.Condition {
		case NeedsPull, Synced:
			return nil
		case SyncFromRemoteIncomplete:
			if !canSafelyRetryIncomplete(status.Condition, direction, status.LocalSyncRecord, status.RemoteSyncRecord) {
				return unsafe(status, req, "")
			}
			return nil
		default:
			return unsafe(status, req, "")
		}
	}
	return fmt.Errorf("Unknown sync direction: %s", direction)
}

// performSync writes the sync records and moves the data.
//
// The ORDER here is the whole safety mechanism, so it must not be rearranged:
//
//   - A PUSH writes an INCOMPLETE record to BOTH sides first. If it is
//     interrupted, both sides carry the same incomplete ULID, which is the
//     proof that this machine owns the interrupted sync and may retry it.
//   - A PULL writes an incomplete record only LOCALLY, because only the local
//     side is at risk.
//   - The completed record is written only after rclone reports success.
func performSync(ctx context.Context, s Storage, p Perms, req HelperRequest, status SyncStatus, direction enums.SyncDirection) (SyncStatus, bool, error) {
	rec := models.NewSyncRecord(false, req.SyncerHostname)
	backupName := rec.ULID

	doSync := func(source, sourcePath, dest, destPath, backupRemote, backupPath string) (bool, string, string, error) {
		if !status.IsDir {
			// rclone sync will not accept a file as the destination path.
			destPath = parentPath(destPath, dest == "")
		}
		if req.Verbose {
			fmt.Printf("Syncing %s:%s to %s:%s.  Backup path: %s:%s\n",
				source, sourcePath, dest, destPath, backupRemote, backupPath)
		}
		if err := s.Mkdir(ctx, backupRemote, backupPath); err != nil {
			return false, "", "", err
		}
		fullBackup := backupPath
		if backupRemote != "" {
			fullBackup = backupRemote + ":" + backupPath
		}
		return s.Sync(ctx, SyncOptions{
			Source: source, SourcePath: sourcePath,
			Dest: dest, DestPath: destPath,
			BackupPath: fullBackup,
			Include:    req.Include, Exclude: req.Exclude, Filter: req.Filter,
			IncludeFile: req.IncludePath, ExcludeFile: req.ExcludePath, FiltersFile: req.FiltersPath,
			ShowProgress: req.ShowRcloneProgress,
		})
	}

	var (
		ok             bool
		stdout, stderr string
		err            error
		backupRemote   string
		backupPath     string
	)

	switch direction {
	case enums.DirectionPull:
		if err := s.WriteSyncRecord(ctx, "", req.LocalSyncRecordPath, rec); err != nil {
			return status, false, err
		}
		backupRemote = ""
		backupPath = filepath.Join(req.LocalSyncBackupsPath, backupName)

		ok, stdout, stderr, err = doSync(req.Remote, req.RemotePath, "", req.LocalPath, backupRemote, backupPath)
		if err != nil {
			return status, false, err
		}
		if ok {
			// Restore the exec bits the transport dropped, from the manifest
			// just pulled in. Additive-only; a no-op when absent.
			if req.PreserveExecPerms && status.IsDir {
				if _, err := p.Apply(req.LocalPath); err != nil {
					return status, false, err
				}
			}
			remoteRec, err := s.ReadSyncRecord(ctx, req.Remote, req.RemoteSyncRecordPath)
			if err != nil {
				return status, false, err
			}
			if remoteRec == nil {
				// The Python would crash here with an AttributeError on None.
				// Reachable only if the remote record vanishes mid-sync.
				return status, false, fmt.Errorf(
					"pull succeeded but the remote sync record at '%s' has disappeared; "+
						"the local sync record is left incomplete and the pull can be retried",
					req.RemoteSyncRecordPath)
			}
			if err := s.WriteSyncRecord(ctx, "", req.LocalSyncRecordPath, *remoteRec); err != nil {
				return status, false, err
			}
		}

	case enums.DirectionPush:
		// Capture the current exec bits so they travel with the data; the
		// transport cannot carry Unix mode over SFTP. Only rewrites when
		// something changed, so a clean box does not churn.
		if req.PreserveExecPerms && status.IsDir {
			if _, err := p.Generate(req.LocalPath); err != nil {
				return status, false, err
			}
		}
		if err := s.WriteSyncRecord(ctx, req.Remote, req.RemoteSyncRecordPath, rec); err != nil {
			return status, false, err
		}
		if err := s.WriteSyncRecord(ctx, "", req.LocalSyncRecordPath, rec); err != nil {
			return status, false, err
		}
		backupRemote = req.Remote
		backupPath = path.Join(req.RemoteSyncBackupsPath, backupName)

		ok, stdout, stderr, err = doSync("", req.LocalPath, req.Remote, req.RemotePath, backupRemote, backupPath)
		if err != nil {
			return status, false, err
		}
		if ok {
			done := models.NewSyncRecord(true, req.SyncerHostname)
			if err := s.WriteSyncRecord(ctx, "", req.LocalSyncRecordPath, done); err != nil {
				return status, false, err
			}
			if err := s.WriteSyncRecord(ctx, req.Remote, req.RemoteSyncRecordPath, done); err != nil {
				return status, false, err
			}
		}

	default:
		return status, false, fmt.Errorf("Unknown sync direction: %s", direction)
	}

	if !ok {
		// The incomplete records stay put on purpose: they are what tells the
		// next run that a sync was interrupted, and by whom.
		return status, false, &SyncFailedError{Stdout: stdout, Stderr: stderr}
	}

	if req.DeleteBackup {
		if err := s.Purge(ctx, backupRemote, backupPath); err != nil {
			return status, false, err
		}
	}
	return status, true, nil
}

// parentPath returns the directory containing p, matching the Python's
// Path(dest_path).parent.as_posix() with "." normalised to "".
func parentPath(p string, isLocal bool) string {
	var parent string
	if isLocal {
		parent = filepath.Dir(p)
	} else {
		parent = path.Dir(p)
	}
	if parent == "." {
		return ""
	}
	return parent
}

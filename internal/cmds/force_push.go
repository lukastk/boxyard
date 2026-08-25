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
)

// ForcePushOptions mirrors the Python `force_push_to_remote` signature.
type ForcePushOptions struct {
	BoxIndexName string
	SourcePath   string
	// Force must be set. Without it the command refuses: this overwrites the
	// remote DATA outright.
	Force              bool
	ShowRcloneProgress bool
	Verbose            bool
	Out                io.Writer
}

// ForcePushToRemote overwrites a box's remote DATA from an arbitrary directory.
//
// Ported from pts/mod/cmds/13_force_push_to_remote.pct.py. It bypasses the sync
// engine entirely, which is why it repeats the sync-record protocol by hand —
// and why it needs its own ownership gate.
func ForcePushToRemote(ctx context.Context, cfg *config.Config, s SyncStore, p syncengine.Perms, opts ForcePushOptions) error {
	if !opts.Force {
		return fmt.Errorf(
			"This is a destructive operation that will overwrite the remote DATA. " +
				"You must pass --force to confirm.")
	}
	printf := func(format string, a ...any) {
		if opts.Out != nil && opts.Verbose {
			fmt.Fprintf(opts.Out, format, a...)
		}
	}

	sourcePath, err := filepath.Abs(opts.SourcePath)
	if err != nil {
		return err
	}
	info, err := os.Stat(sourcePath)
	if os.IsNotExist(err) {
		return fmt.Errorf("Source path '%s' does not exist.", sourcePath)
	}
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return fmt.Errorf("Source path '%s' is not a directory.", sourcePath)
	}

	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return err
	}
	bm, ok := meta.ByIndexName()[opts.BoxIndexName]
	if !ok {
		return fmt.Errorf("Box '%s' does not exist locally.", opts.BoxIndexName)
	}

	// Ownership is checked BEFORE and INDEPENDENTLY of the force flag. A
	// --force that also bypassed ownership would leave the remote holding this
	// machine's data while boxmeta.toml still names another machine as the
	// owner — a lie in shared state, which is worse than a refusal.
	onDisk, err := models.LoadBoxMeta(cfg, bm.StorageLocation, opts.BoxIndexName)
	if err != nil {
		return err
	}
	if err := ownership.OwnerGate(cfg, onDisk,
		fmt.Sprintf("force-push to '%s'", opts.BoxIndexName)); err != nil {
		return err
	}

	slConfig, err := bm.StorageLocationConfig(cfg)
	if err != nil {
		return err
	}
	remoteIndexName, err := remoteindex.Find(ctx, s.ForRemoteIndex(), cfg, bm.StorageLocation, bm.BoxID())
	if err != nil {
		return err
	}
	if remoteIndexName == "" {
		return fmt.Errorf(
			"Box '%s' not found on remote storage '%s'. The box may have been deleted or the "+
				"remote is not accessible.",
			opts.BoxIndexName, bm.StorageLocation)
	}
	printf("Found remote box: %s\n", remoteIndexName)

	remoteDataPath := remotePartPath(slConfig.StorePath, remoteIndexName, enums.PartData)
	remoteRecordPath := remoteSyncRecordPath(slConfig.StorePath, remoteIndexName, enums.PartData)
	localRecordPath := bm.LocalSyncRecordPath(cfg, enums.PartData)
	remoteBackupsPath := path.Join(slConfig.StorePath, boxconst.RemoteBackupRelPath,
		remoteIndexName, string(enums.PartData))

	mgr := locking.NewManager(cfg.BoxyardDataPath)
	release, err := mgr.BoxSyncLock(opts.BoxIndexName, locking.BoxSyncLockTimeout)
	if err != nil {
		return err
	}
	defer release()

	if err := ctx.Err(); err != nil {
		return err
	}
	printf("Force pushing %s to %s:%s\n", sourcePath, bm.StorageLocation, remoteDataPath)

	// The sync-record protocol, by hand. An INCOMPLETE record goes to BOTH
	// sides first with the same ULID: if this is interrupted, that shared ULID
	// is the proof this machine owns the interrupted sync and may retry it.
	rec := models.NewSyncRecord(false, "")
	printf("Creating sync session with ULID: %s\n", rec.ULID)
	if err := s.WriteSyncRecord(ctx, bm.StorageLocation, remoteRecordPath, rec); err != nil {
		return err
	}
	if err := s.WriteSyncRecord(ctx, "", localRecordPath, rec); err != nil {
		return err
	}

	backupPath := path.Join(remoteBackupsPath, rec.ULID)
	if err := s.Mkdir(ctx, bm.StorageLocation, backupPath); err != nil {
		return err
	}

	printf("Syncing data to remote...\n")
	// Capture the exec bits into the manifest so +x survives a transport that
	// cannot carry Unix mode.
	if _, err := p.Generate(sourcePath); err != nil {
		return err
	}
	ok2, _, stderr, err := s.Sync(ctx, syncengine.SyncOptions{
		Source: "", SourcePath: sourcePath,
		Dest: bm.StorageLocation, DestPath: remoteDataPath,
		BackupPath:   bm.StorageLocation + ":" + backupPath,
		ShowProgress: opts.ShowRcloneProgress,
	})
	if err != nil {
		return err
	}
	if !ok2 {
		// The incomplete records are deliberately LEFT: they are what tells the
		// next run that a sync was interrupted, and by whom.
		return fmt.Errorf("Failed to sync to remote: %s", stderr)
	}
	printf("Sync completed successfully.\n")

	complete := models.NewSyncRecord(true, "")
	if err := s.WriteSyncRecord(ctx, "", localRecordPath, complete); err != nil {
		return err
	}
	if err := s.WriteSyncRecord(ctx, bm.StorageLocation, remoteRecordPath, complete); err != nil {
		return err
	}
	printf("Sync records updated.\n")

	if err := s.Purge(ctx, bm.StorageLocation, backupPath); err != nil {
		return err
	}
	printf("Backup cleaned up.\nForce push complete: %s -> %s:%s\n",
		sourcePath, bm.StorageLocation, remoteDataPath)
	return nil
}

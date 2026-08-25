package cmds

import (
	"context"
	"errors"
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
	"github.com/lukastk/boxyard/internal/tombstones"
)

// DeleteBoxOptions mirrors the Python `delete_box` signature.
type DeleteBoxOptions struct {
	BoxIndexName string
	Out          io.Writer
}

// DeleteBox removes a box from this machine AND from the remote, leaving a
// tombstone so other machines do not resurrect it.
//
// Ported from pts/mod/cmds/08_delete_box.pct.py.
func DeleteBox(ctx context.Context, cfg *config.Config, s SyncStore, opts DeleteBoxOptions) error {
	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return err
	}
	bm, ok := meta.ByIndexName()[opts.BoxIndexName]
	if !ok {
		return fmt.Errorf("Box '%s' does not exist.", opts.BoxIndexName)
	}

	// Ownership is checked BEFORE and INDEPENDENTLY of any force/safety flag.
	// `delete` purges the REMOTE and writes a tombstone keyed by box id, so it
	// takes the box away from every machine, not just this one.
	onDisk, err := models.LoadBoxMeta(cfg, bm.StorageLocation, opts.BoxIndexName)
	if err != nil {
		return err
	}
	if err := ownership.OwnerGate(cfg, onDisk, fmt.Sprintf("delete '%s'", opts.BoxIndexName)); err != nil {
		return err
	}

	slConfig, err := bm.StorageLocationConfig(cfg)
	if err != nil {
		return err
	}
	isRemote := slConfig.StorageType != config.StorageLocal
	boxID := bm.BoxID()

	mgr := locking.NewManager(cfg.BoxyardDataPath)
	release, err := mgr.BoxSyncLock(opts.BoxIndexName, locking.BoxSyncLockTimeout)
	if err != nil {
		return err
	}
	defer release()

	// Delete the LOCAL box first — before the tombstone or the remote purge.
	// If a file is owned by another user (root, a container uid, …) the removal
	// fails; doing it first means aborting cleanly with no partial delete, and
	// an actionable error rather than a half-deleted box.
	dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		return err
	}
	for _, p := range []string{dataPath, bm.LocalPath(cfg)} {
		if err := removeTreeWithRetry(p); err != nil {
			if errors.Is(err, os.ErrPermission) {
				return fmt.Errorf(
					"Cannot delete box '%s': some local files are owned by another user and "+
						"can't be removed (%w). Fix ownership and retry, e.g.  "+
						"sudo chown -R \"$USER\" %s",
					opts.BoxIndexName, err, dataPath)
			}
			return err
		}
	}

	if isRemote {
		// The tombstone goes up BEFORE the remote is purged, so a machine that
		// syncs in between sees "deleted" rather than "vanished".
		if _, err := tombstones.Create(ctx, s, cfg, bm.StorageLocation, boxID, bm.Name); err != nil {
			return err
		}
		remotePath, err := bm.RemotePath(cfg)
		if err != nil {
			return err
		}
		if err := s.Purge(ctx, bm.StorageLocation, remotePath); err != nil {
			return err
		}
	}

	// Sync records and backups go too, on both sides: a delete that leaves them
	// behind leaves orphans, which is what doctor used to report.
	localRecordDir := filepath.Dir(bm.LocalSyncRecordPath(cfg, enums.PartData))
	if err := removeTreeWithRetry(localRecordDir); err != nil {
		return err
	}
	if err := removeTreeWithRetry(filepath.Join(cfg.LocalSyncBackupsPath(), bm.IndexName())); err != nil {
		return err
	}
	if isRemote {
		for _, rel := range []string{boxconst.SyncRecordsRelPath, boxconst.RemoteBackupRelPath} {
			if err := s.Purge(ctx, bm.StorageLocation,
				path.Join(slConfig.StorePath, rel, bm.IndexName())); err != nil {
				return err
			}
		}
		if err := remoteindex.Remove(cfg, bm.StorageLocation, boxID); err != nil {
			return err
		}
	}

	if _, err := models.RefreshBoxyardMeta(cfg, false); err != nil {
		return err
	}
	if opts.Out != nil {
		fmt.Fprintf(opts.Out, "Deleted box '%s'\n", bm.Name)
	}
	return nil
}

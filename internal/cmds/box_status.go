package cmds

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/syncengine"
)

// BoxStatuser is the storage access BoxSyncStatus needs. It is the state
// machine's own Prober — named here so callers can see what must be supplied.
type BoxStatuser = syncengine.Prober

// BoxSyncStatus probes every part of one box.
//
// Ported from pts/mod/cmds/02_get_box_sync_status.pct.py.
func BoxSyncStatus(ctx context.Context, cfg *config.Config, p BoxStatuser, boxIndexName string) (map[enums.BoxPart]syncengine.SyncStatus, error) {
	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return nil, err
	}
	bm, ok := meta.ByIndexName()[boxIndexName]
	if !ok {
		return nil, fmt.Errorf("Box '%s' not found.", boxIndexName)
	}

	slConfig, err := bm.StorageLocationConfig(cfg)
	if err != nil {
		return nil, err
	}
	// A `local` storage location has no rclone remote: `remote` is passed to
	// rclone as a remote NAME, and a local location has no section in
	// boxyard_rclone.conf. Python probed anyway and died with "didn't find
	// section in config file" — so `box-status`, `yard-status` and
	// `list --status` all failed on such a box (fixed in v0.5.5).
	if slConfig.StorageType == config.StorageLocal {
		record := models.NewSyncRecord(false, "")
		out := make(map[enums.BoxPart]syncengine.SyncStatus, len(enums.AllBoxParts))
		for _, part := range enums.AllBoxParts {
			out[part] = syncengine.SyncStatus{
				Condition:        syncengine.LocalStorage,
				LocalPathExists:  true,
				RemotePathExists: false,
				LocalSyncRecord:  &record,
				RemoteSyncRecord: &record,
				IsDir:            true,
			}
		}
		return out, nil
	}

	excludePath, err := EffectiveExcludePath(cfg, bm)
	if err != nil {
		return nil, err
	}

	out := make(map[enums.BoxPart]syncengine.SyncStatus, len(enums.AllBoxParts))
	for _, part := range enums.AllBoxParts {
		localPath, err := bm.LocalPartPath(cfg, part)
		if err != nil {
			return nil, err
		}
		remotePath, err := bm.RemotePartPath(cfg, part)
		if err != nil {
			return nil, err
		}
		remoteRecordPath, err := bm.RemoteSyncRecordPath(cfg, part)
		if err != nil {
			return nil, err
		}
		status, err := syncengine.GetSyncStatus(ctx, p, syncengine.StatusRequest{
			LocalPath:            localPath,
			LocalSyncRecordPath:  bm.LocalSyncRecordPath(cfg, part),
			Remote:               bm.StorageLocation,
			RemotePath:           remotePath,
			RemoteSyncRecordPath: remoteRecordPath,
			ExcludePath:          excludePath,
			// Python's get_box_sync_status passes no
			// local_absence_means_excluded, so every part gets the DATA
			// meaning here — including CONF. That is deliberate on the Python
			// side: v0.5.3 changed what sync_box ACTS on, not what box-status
			// REPORTS, and reporting CONF as NEEDS_PULL would show a pending
			// change for something the next sync silently resolves.
			TreatLocalAbsenceAsNeedsPull: false,
		})
		if err != nil {
			return nil, err
		}
		out[part] = status
	}
	return out, nil
}

// EffectiveExcludePath returns the exclude file that actually governs a box:
// its own conf/.rclone_exclude if it has one, else the global default.
//
// A per-box exclude file REPLACES the global one rather than adding to it, so
// assuming the defaults for a box that overrides them could prune a directory
// the box really does sync — hiding a genuine change.
func EffectiveExcludePath(cfg *config.Config, bm *models.BoxMeta) (string, error) {
	confPath, err := bm.LocalPartPath(cfg, enums.PartConf)
	if err != nil {
		return "", err
	}
	boxExclude := filepath.Join(confPath, boxconst.RcloneExcludeFilename)
	if _, err := os.Stat(boxExclude); err == nil {
		return boxExclude, nil
	} else if !os.IsNotExist(err) {
		return "", err
	}
	return cfg.DefaultRcloneExcludePath(), nil
}

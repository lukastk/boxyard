package cmds

import (
	"context"
	"fmt"
	"io"
	"path"
	"path/filepath"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/locking"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/remoteindex"
	"github.com/lukastk/boxyard/internal/syncengine"
)

// DiscardLocalOptions mirrors the Python `discard_local` signature.
type DiscardLocalOptions struct {
	BoxIndexName       string
	ShowRcloneProgress bool
	Verbose            bool
	Out                io.Writer
}

// DiscardLocal throws away this machine's local changes to a box and takes the
// remote copy. It returns the directory the overwritten files were kept in.
//
// Ported from pts/mod/cmds/17_discard_local.pct.py. This is one of the two ways
// out of WRITE_DENIED, so it has to be genuinely recoverable — nothing is
// deleted outright.
func DiscardLocal(ctx context.Context, cfg *config.Config, s SyncStore, p syncengine.Perms, opts DiscardLocalOptions) (string, error) {
	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return "", err
	}
	bm, ok := meta.ByIndexName()[opts.BoxIndexName]
	if !ok {
		return "", fmt.Errorf("Box '%s' not found.", opts.BoxIndexName)
	}
	slConfig, err := bm.StorageLocationConfig(cfg)
	if err != nil {
		return "", err
	}
	if slConfig.StorageType == config.StorageLocal {
		return "", fmt.Errorf(
			"Box '%s' is in local storage location '%s'; there is no remote copy to take.",
			opts.BoxIndexName, bm.StorageLocation)
	}
	if !bm.CheckIncluded(cfg) {
		return "", fmt.Errorf(
			"Box '%s' is not included on this machine, so there are no local changes to discard.",
			opts.BoxIndexName)
	}

	remoteIndexName, err := remoteindex.Find(ctx, s.ForRemoteIndex(), cfg, bm.StorageLocation, bm.BoxID())
	if err != nil {
		return "", err
	}
	if remoteIndexName == "" {
		remoteIndexName = opts.BoxIndexName
	}

	confPath, err := bm.LocalPartPath(cfg, enums.PartConf)
	if err != nil {
		return "", err
	}
	includePath := existingOrEmpty(filepath.Join(confPath, boxconst.RcloneIncludeFilename))
	excludePath := existingOrEmpty(filepath.Join(confPath, boxconst.RcloneExcludeFilename))
	if excludePath == "" {
		excludePath = cfg.DefaultRcloneExcludePath()
	}
	filtersPath := existingOrEmpty(filepath.Join(confPath, boxconst.RcloneFiltersFilename))

	localPath, err := bm.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		return "", err
	}

	mgr := locking.NewManager(cfg.BoxyardDataPath)
	release, err := mgr.BoxSyncLock(opts.BoxIndexName, locking.BoxSyncLockTimeout)
	if err != nil {
		return "", err
	}
	defer release()

	pull := enums.DirectionPull
	if _, _, err := syncengine.Run(ctx, s, p, syncengine.HelperRequest{
		StatusRequest: syncengine.StatusRequest{
			LocalPath:            localPath,
			LocalSyncRecordPath:  bm.LocalSyncRecordPath(cfg, enums.PartData),
			Remote:               bm.StorageLocation,
			RemotePath:           remotePartPath(slConfig.StorePath, remoteIndexName, enums.PartData),
			RemoteSyncRecordPath: remoteSyncRecordPath(slConfig.StorePath, remoteIndexName, enums.PartData),
			ExcludePath:          excludePath,
		},
		Direction:             &pull,
		Setting:               enums.SyncForce,
		LocalSyncBackupsPath:  cfg.LocalSyncBackupsPath(),
		RemoteSyncBackupsPath: path.Join(slConfig.StorePath, boxconst.RemoteBackupRelPath),
		IncludePath:           includePath,
		ExcludePath:           excludePath,
		FiltersPath:           filtersPath,
		// The point of the command: what is thrown away must be RECOVERABLE.
		// This is one of the two ways out of WRITE_DENIED, and a way out that
		// destroys work is one people refuse to take.
		DeleteBackup:       false,
		ShowRcloneProgress: opts.ShowRcloneProgress,
		PreserveExecPerms:  true,
	}); err != nil {
		return "", err
	}

	backupsPath := cfg.LocalSyncBackupsPath()
	if opts.Out != nil && opts.Verbose {
		fmt.Fprintf(opts.Out,
			"Discarded this machine's local changes to '%s' and took the remote copy.\n"+
				"What was overwritten is under '%s' — nothing was deleted outright. "+
				"Look there before assuming the work is gone.\n",
			opts.BoxIndexName, backupsPath)
	}
	return backupsPath, nil
}

package cli

import (
	"os"

	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/storage"
	"github.com/spf13/cobra"
)

func newSyncMissingMetaCommand() *cobra.Command {
	var (
		boxIndexNames       []string
		storageLocations    []string
		syncSetting         string
		syncDirection       string
		maxConcurrent       int
		refreshUserSymlinks bool
		noRefreshSymlinks   bool
		softInterruption    bool
		noSoftInterruption  bool
	)

	cmd := &cobra.Command{
		Use:   "sync-missing-meta",
		Short: "Sync boxmetas on remote storage locations not yet present locally",
		RunE: func(cmd *cobra.Command, args []string) error {
			if noRefreshSymlinks {
				refreshUserSymlinks = false
			}
			if noSoftInterruption {
				softInterruption = false
			}
			// --sync-setting and --sync-direction are accepted for surface
			// parity but not yet acted on: the discovery pass is one listing
			// plus one filtered fetch per storage location, so neither has
			// anything to apply to. They are still enum flags, so a typo in
			// one fails at parse time rather than being silently accepted.

			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			client, err := rclone.New(cfg.RcloneConfigPath())
			if err != nil {
				return err
			}
			ctx, stop := maybeSoftInterrupt(softInterruption)
			defer stop()

			if err := cmds.SyncMissingBoxMetas(ctx, cfg, storage.New(client),
				cmds.SyncMissingBoxMetasOptions{
					BoxIndexNames:    boxIndexNames,
					StorageLocations: storageLocations,
					Verbose:          true,
					Out:              os.Stdout,
				}); err != nil {
				return err
			}
			return maybeRefreshSymlinks(cfg, refreshUserSymlinks)
		},
	}

	f := cmd.Flags()
	f.StringArrayVarP(&boxIndexNames, "box", "r", nil, "The index name of the box to sync.")
	f.StringArrayVarP(&storageLocations, "storage-location", "s", nil, "The storage location to sync.")
	enumVar(f, &syncSetting, "sync-setting", "", string(enums.SyncCareful), "The sync setting to use.", enums.SyncSettingNames)
	enumVar(f, &syncDirection, "sync-direction", "d", "", "The direction of the sync.", enums.SyncDirectionNames)
	f.IntVarP(&maxConcurrent, "max-concurrent", "m", 0, "The maximum number of concurrent rclone operations.")
	f.BoolVar(&refreshUserSymlinks, "refresh-user-symlinks", true, "Refresh the user symlinks.")
	f.BoolVar(&noRefreshSymlinks, "no-refresh-user-symlinks", false, "Do not refresh the user symlinks.")
	f.BoolVar(&softInterruption, "soft-interruption-enabled", true, "Enable soft interruption.")
	f.BoolVar(&noSoftInterruption, "no-soft-interruption-enabled", false, "Disable soft interruption.")
	return cmd
}

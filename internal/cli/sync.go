package cli

import (
	"context"
	"errors"
	"fmt"
	"os"

	"github.com/lukastk/boxyard/internal/boxref"
	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/perms"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/storage"
	"github.com/lukastk/boxyard/internal/symlinks"
	"github.com/spf13/cobra"
)

func newSyncCommand() *cobra.Command {
	var (
		sel                 boxSelectorFlags
		syncDirection       string
		syncSetting         string
		syncChoices         []string
		showRcloneProgress  bool
		refreshUserSymlinks bool
		noRefreshSymlinks   bool
		syncChildren        bool
		softInterruption    bool
		noSoftInterruption  bool
	)

	cmd := &cobra.Command{
		Use:   "sync",
		Short: "Sync a box",
		RunE: func(cmd *cobra.Command, args []string) error {
			if noRefreshSymlinks {
				refreshUserSymlinks = false
			}
			if noSoftInterruption {
				softInterruption = false
			}

			direction, err := parseDirection(syncDirection)
			if err != nil {
				return err
			}
			setting := enums.SyncSetting(syncSetting) // validated at parse time
			parts, err := parseBoxParts(syncChoices)
			if err != nil {
				return err
			}

			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{AllowNoArgs: true})
			if err != nil {
				return handleResolveError(err)
			}

			client, err := rclone.New(cfg.RcloneConfigPath())
			if err != nil {
				return err
			}
			store := storage.New(client)

			ctx := context.Background()
			if softInterruption {
				var stop func()
				ctx, stop = softInterrupt(ctx)
				defer stop()
			}

			targets := []string{indexName}
			if syncChildren {
				bm, ok := meta.ByIndexName()[indexName]
				if !ok {
					return fmt.Errorf("Box '%s' not found.", indexName)
				}
				for _, d := range meta.DescendantsOf(bm.BoxID()) {
					targets = append(targets, d.IndexName())
				}
			}

			for i, target := range targets {
				if i > 0 {
					fmt.Printf("Syncing child: %s\n", target)
				}
				_, err := cmds.SyncBox(ctx, cfg, store, perms.Adapter{}, cmds.SyncBoxOptions{
					BoxIndexName:       target,
					Direction:          direction,
					Setting:            setting,
					Choices:            parts,
					Verbose:            true,
					ShowRcloneProgress: showRcloneProgress,
					Out:                os.Stdout,
				})
				if err != nil {
					if errors.Is(err, context.Canceled) {
						// A soft interrupt is the user's decision, not a
						// failure: the box stopped at a part boundary, which is
						// where stopping is safe.
						fmt.Fprintln(os.Stderr, "Interrupted.")
						return nil
					}
					return err
				}
			}

			if refreshUserSymlinks {
				fresh, err := models.GetBoxyardMeta(cfg, false)
				if err != nil {
					return err
				}
				return symlinks.Build(cfg, fresh)
			}
			return nil
		},
	}

	// `sync` spends `-c` on --sync-choices, so --name-match-case has no short
	// flag here. That is the Python's arrangement, not drift.
	sel.register(cmd, selectorSpec{Noun: "sync", WithBoxPath: true})
	f := cmd.Flags()
	enumVar(f, &syncDirection, "sync-direction", "d", "",
		"The direction of the sync. If not provided, the appropriate direction will be automatically determined based on the sync status. This mode is only available for the 'CAREFUL' sync setting.",
		enums.SyncDirectionNames)
	enumVar(f, &syncSetting, "sync-setting", "s", string(enums.SyncCareful), "The sync setting to use.", enums.SyncSettingNames)
	enumSliceVar(f, &syncChoices, "sync-choices", "c",
		"The parts of the box to sync. If not provided, all parts will be synced. By default, all parts are synced.",
		enums.BoxPartNames)
	f.BoolVar(&showRcloneProgress, "progress", false, "Show the progress of the sync in rclone.")
	f.BoolVar(&refreshUserSymlinks, "refresh-user-symlinks", true, "Refresh the user symlinks.")
	f.BoolVar(&noRefreshSymlinks, "no-refresh-user-symlinks", false, "Do not refresh the user symlinks.")
	f.BoolVar(&syncChildren, "sync-children", false, "Also sync all descendant boxes after syncing the target.")
	f.BoolVar(&softInterruption, "soft-interruption-enabled", true, "Enable soft interruption.")
	f.BoolVar(&noSoftInterruption, "no-soft-interruption-enabled", false, "Disable soft interruption.")
	return cmd
}

// parseDirection returns nil for "", which means "decide from the status".
func parseDirection(s string) (*enums.SyncDirection, error) {
	if s == "" {
		return nil, nil
	}
	d := enums.SyncDirection(s)
	if !d.Valid() {
		return nil, &usageError{err: fmt.Errorf("invalid sync direction: %q", s)}
	}
	return &d, nil
}

// parseBoxParts returns nil for an empty selection, which means "all parts".
func parseBoxParts(values []string) ([]enums.BoxPart, error) {
	if len(values) == 0 {
		return nil, nil
	}
	out := make([]enums.BoxPart, 0, len(values))
	for _, v := range values {
		part := enums.BoxPart(v)
		if !part.Valid() {
			return nil, &usageError{err: fmt.Errorf("invalid box part: %q", v)}
		}
		out = append(out, part)
	}
	return out, nil
}

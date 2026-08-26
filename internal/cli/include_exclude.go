package cli

import (
	"context"
	"fmt"
	"os"

	"github.com/lukastk/boxyard/internal/boxref"
	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/perms"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/storage"
	"github.com/lukastk/boxyard/internal/symlinks"
	"github.com/spf13/cobra"
)

func newIncludeCommand() *cobra.Command {
	var (
		sel                 boxSelectorFlags
		interactive         bool
		refreshUserSymlinks bool
		noRefreshSymlinks   bool
		softInterruption    bool
		noSoftInterruption  bool
		readOnly            bool
	)

	cmd := &cobra.Command{
		Use:   "include",
		Short: "Include a box in the local store",
		RunE: func(cmd *cobra.Command, args []string) error {
			if noRefreshSymlinks {
				refreshUserSymlinks = false
			}
			if noSoftInterruption {
				softInterruption = false
			}

			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}

			// Only EXCLUDED boxes are candidates: including one already here is
			// a no-op the command refuses, so offering it would be a dead end.
			var eligible []*models.BoxMeta
			for _, bm := range meta.BoxMetas {
				if !bm.CheckIncluded(cfg) {
					eligible = append(eligible, bm)
				}
			}

			if interactive {
				ctx, stop := maybeSoftInterrupt(softInterruption)
				defer stop()
				store, err := newStore(cfg)
				if err != nil {
					return err
				}
				candidates := make([]pickCandidate, len(eligible))
				for i, bm := range eligible {
					candidates[i] = pickCandidate{bm: bm}
				}
				sortByName(candidates)

				chosen, err := interactivePick(candidates,
					func(c pickCandidate) string { return includePickLine(c.bm) },
					"No excluded boxes to include.", "include",
					func(c pickCandidate) string { return includeConfirmLine(c.bm) },
					nil, os.Stdout, os.Stdin)
				if err != nil || len(chosen) == 0 {
					return err
				}

				if err := runBatch(chosen, "Including", func(bm *models.BoxMeta) error {
					return cmds.IncludeBox(ctx, cfg, store, perms.Adapter{}, cmds.IncludeBoxOptions{
						BoxIndexName: bm.IndexName(),
						ReadOnly:     readOnly,
						Out:          os.Stdout,
					})
				}, os.Stdout, os.Stderr); err != nil {
					return err
				}
				return maybeRefreshSymlinks(cfg, refreshUserSymlinks)
			}

			indexName, err := sel.resolve(cfg, eligible, boxref.Options{AllowNoArgs: true})
			if err != nil {
				return handleResolveError(err)
			}
			if _, ok := meta.ByIndexName()[indexName]; !ok {
				fmt.Printf("Box with index name `%s` not found.\n", indexName)
				os.Exit(1)
			}

			ctx, stop := maybeSoftInterrupt(softInterruption)
			defer stop()
			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			if err := cmds.IncludeBox(ctx, cfg, store, perms.Adapter{}, cmds.IncludeBoxOptions{
				BoxIndexName: indexName,
				ReadOnly:     readOnly,
				Out:          os.Stdout,
			}); err != nil {
				return err
			}
			return maybeRefreshSymlinks(cfg, refreshUserSymlinks)
		},
	}

	// `include` has no --box-path: the box is not on this machine yet, so
	// there is no local path to point at.
	sel.register(cmd, selectorSpec{Noun: "include", MatchCaseShort: "c"})
	f := cmd.Flags()
	f.BoolVarP(&interactive, "interactive", "I", false, "Interactively pick boxes to include.")
	f.BoolVar(&refreshUserSymlinks, "refresh-user-symlinks", true, "Refresh the user symlinks.")
	f.BoolVar(&noRefreshSymlinks, "no-refresh-user-symlinks", false, "Do not refresh the user symlinks.")
	f.BoolVar(&softInterruption, "soft-interruption-enabled", true, "Enable soft interruption.")
	f.BoolVar(&noSoftInterruption, "no-soft-interruption-enabled", false, "Disable soft interruption.")
	f.BoolVar(&readOnly, "read-only", false, "Include without the nudge to claim an unowned box.")
	return cmd
}

func newExcludeCommand() *cobra.Command {
	var (
		sel                 boxSelectorFlags
		skipSync            bool
		interactive         bool
		showSizes           bool
		refreshUserSymlinks bool
		noRefreshSymlinks   bool
		softInterruption    bool
		noSoftInterruption  bool
	)

	cmd := &cobra.Command{
		Use:   "exclude",
		Short: "Exclude a box from the local store",
		RunE: func(cmd *cobra.Command, args []string) error {
			if noRefreshSymlinks {
				refreshUserSymlinks = false
			}
			if noSoftInterruption {
				softInterruption = false
			}

			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}

			// Included, non-local boxes only. A local storage location IS the
			// local copy, so excluding one would delete the only copy there is
			// — the command refuses it, so it is not a candidate either.
			var eligible []*models.BoxMeta
			for _, bm := range meta.BoxMetas {
				if !bm.CheckIncluded(cfg) {
					continue
				}
				slConfig, err := bm.StorageLocationConfig(cfg)
				if err != nil {
					return err
				}
				if slConfig.StorageType == config.StorageLocal {
					continue
				}
				eligible = append(eligible, bm)
			}

			if interactive {
				ctx, stop := maybeSoftInterrupt(softInterruption)
				defer stop()
				store, err := newStore(cfg)
				if err != nil {
					return err
				}
				candidates := make([]pickCandidate, len(eligible))
				for i, bm := range eligible {
					candidates[i] = pickCandidate{bm: bm}
					if showSizes {
						dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
						if err != nil {
							return err
						}
						if _, statErr := os.Stat(dataPath); statErr == nil {
							candidates[i].size = dirSize(dataPath)
						}
					}
				}
				if showSizes {
					sortBySizeDesc(candidates)
				} else {
					sortByName(candidates)
				}

				sizePrefix := func(c pickCandidate) string {
					if !showSizes {
						return ""
					}
					return "[" + formatSize(c.size) + "]  "
				}
				var totalLine func([]pickCandidate) string
				if showSizes {
					totalLine = func(chosen []pickCandidate) string {
						var total int64
						for _, c := range chosen {
							total += c.size
						}
						return "Total: " + formatSize(total)
					}
				}

				chosen, err := interactivePick(candidates,
					func(c pickCandidate) string { return excludePickLine(c.bm, sizePrefix(c)) },
					"No eligible boxes to exclude.", "exclude",
					func(c pickCandidate) string { return excludeConfirmLine(c.bm, sizePrefix(c)) },
					totalLine, os.Stdout, os.Stdin)
				if err != nil || len(chosen) == 0 {
					return err
				}

				if err := runBatch(chosen, "Excluding", func(bm *models.BoxMeta) error {
					return cmds.ExcludeBox(ctx, cfg, store, perms.Adapter{}, cmds.ExcludeBoxOptions{
						BoxIndexName: bm.IndexName(),
						SkipSync:     skipSync,
						Out:          os.Stdout,
					})
				}, os.Stdout, os.Stderr); err != nil {
					return err
				}
				return maybeRefreshSymlinks(cfg, refreshUserSymlinks)
			}

			indexName, err := sel.resolve(cfg, eligible, boxref.Options{AllowNoArgs: true})
			if err != nil {
				return handleResolveError(err)
			}
			if _, ok := meta.ByIndexName()[indexName]; !ok {
				fmt.Printf("Box with index name `%s` not found.\n", indexName)
				os.Exit(1)
			}

			ctx, stop := maybeSoftInterrupt(softInterruption)
			defer stop()
			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			if err := cmds.ExcludeBox(ctx, cfg, store, perms.Adapter{}, cmds.ExcludeBoxOptions{
				BoxIndexName: indexName,
				SkipSync:     skipSync,
				Out:          os.Stdout,
			}); err != nil {
				return err
			}
			return maybeRefreshSymlinks(cfg, refreshUserSymlinks)
		},
	}

	sel.register(cmd, selectorSpec{Noun: "exclude", MatchCaseShort: "c"})
	f := cmd.Flags()
	f.BoolVarP(&skipSync, "skip-sync", "s", false, "Skip the sync before excluding.")
	f.BoolVarP(&interactive, "interactive", "I", false, "Interactively pick boxes to exclude.")
	f.BoolVar(&showSizes, "show-sizes", false, "Show local sizes in the interactive picker.")
	f.BoolVar(&refreshUserSymlinks, "refresh-user-symlinks", true, "Refresh the user symlinks.")
	f.BoolVar(&noRefreshSymlinks, "no-refresh-user-symlinks", false, "Do not refresh the user symlinks.")
	f.BoolVar(&softInterruption, "soft-interruption-enabled", true, "Enable soft interruption.")
	f.BoolVar(&noSoftInterruption, "no-soft-interruption-enabled", false, "Disable soft interruption.")
	return cmd
}

// newStore builds the rclone-backed store the commands need.
//
// It returns the CONCRETE adapter rather than one of the interfaces: the
// command layer declares several narrow ones (SyncStore, RenameStore,
// MetaSyncStore, CopyStore) and a call site should not have to pick which
// alias to hold. The compile-time assertions in internal/cmds are what keep the
// adapter honest.
func newStore(cfg *config.Config) (*storage.Adapter, error) {
	client, err := rclone.New(cfg.RcloneConfigPath())
	if err != nil {
		return nil, err
	}
	return storage.New(client), nil
}

// maybeSoftInterrupt wires Ctrl-C to the context when the flag is on.
func maybeSoftInterrupt(enabled bool) (context.Context, func()) {
	if !enabled {
		return context.Background(), func() {}
	}
	return softInterrupt(context.Background())
}

func maybeRefreshSymlinks(cfg *config.Config, refresh bool) error {
	if !refresh {
		return nil
	}
	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return err
	}
	return symlinks.Build(cfg, meta)
}

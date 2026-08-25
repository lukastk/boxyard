package cli

import (
	"fmt"
	"os"

	"github.com/lukastk/boxyard/internal/boxref"
	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/perms"
	"github.com/spf13/cobra"
)

func newDeleteCommand() *cobra.Command {
	var (
		sel                 boxSelectorFlags
		force               bool
		refreshUserSymlinks bool
		noRefreshSymlinks   bool
		softInterruption    bool
		noSoftInterruption  bool
	)

	cmd := &cobra.Command{
		Use:   "delete",
		Short: "Delete a box",
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
			// AllowNoArgs is FALSE: `delete` purges the remote and writes a
			// tombstone, and it has no confirmation prompt. A bare invocation
			// must refuse, not offer a picker of boxes to destroy.
			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{Label: "box"})
			if err != nil {
				return handleResolveError(err)
			}
			bm, ok := meta.ByIndexName()[indexName]
			if !ok {
				fmt.Printf("Box with index name `%s` not found.\n", indexName)
				os.Exit(1)
			}

			// Children are orphaned by a delete, so they are named and the
			// deletion refused unless --force.
			children := meta.ChildrenOf(bm.BoxID())
			if len(children) > 0 {
				names := make([]string, len(children))
				for i, c := range children {
					names[i] = c.IndexName()
				}
				if !force {
					fmt.Fprintf(os.Stderr, "Box `%s` has children: %s. Use --force to delete anyway.\n",
						indexName, joinStrings(names))
					os.Exit(1)
				}
				fmt.Fprintf(os.Stderr, "Warning: Deleting box `%s` will orphan children: %s\n",
					indexName, joinStrings(names))
			}

			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			ctx, stop := maybeSoftInterrupt(softInterruption)
			defer stop()
			if err := cmds.DeleteBox(ctx, cfg, store, cmds.DeleteBoxOptions{
				BoxIndexName: indexName, Out: os.Stdout,
			}); err != nil {
				return err
			}
			return maybeRefreshSymlinks(cfg, refreshUserSymlinks)
		},
	}
	sel.register(cmd, selectorSpec{Noun: "delete", MatchCaseShort: "c"})
	f := cmd.Flags()
	f.BoolVar(&force, "force", false, "Force deletion even if the box has children.")
	f.BoolVar(&refreshUserSymlinks, "refresh-user-symlinks", true, "Refresh the user symlinks.")
	f.BoolVar(&noRefreshSymlinks, "no-refresh-user-symlinks", false, "Do not refresh the user symlinks.")
	f.BoolVar(&softInterruption, "soft-interruption-enabled", true, "Enable soft interruption.")
	f.BoolVar(&noSoftInterruption, "no-soft-interruption-enabled", false, "Disable soft interruption.")
	return cmd
}

func newRenameCommand() *cobra.Command {
	var (
		sel                 boxSelectorFlags
		newName             string
		scope               string
		refreshUserSymlinks bool
		noRefreshSymlinks   bool
	)

	cmd := &cobra.Command{
		Use:   "rename",
		Short: "Rename a box locally, on remote, or both",
		RunE: func(cmd *cobra.Command, args []string) error {
			if noRefreshSymlinks {
				refreshUserSymlinks = false
			}
			renameScope := enums.RenameScope(scope)
			if !renameScope.Valid() {
				return &usageError{err: fmt.Errorf("invalid rename scope: %q", scope)}
			}
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{Label: "box"})
			if err != nil {
				return handleResolveError(err)
			}
			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			ctx, stop := maybeSoftInterrupt(true)
			defer stop()
			result, err := cmds.RenameBox(ctx, cfg, store, cmds.RenameBoxOptions{
				BoxIndexName: indexName, NewName: newName, Scope: renameScope,
				Verbose: true, Out: os.Stdout,
			})
			if err != nil {
				return err
			}
			fmt.Printf("Result: %s\n", result)
			return maybeRefreshSymlinks(cfg, refreshUserSymlinks)
		},
	}
	sel.register(cmd, selectorSpec{Noun: "rename", MatchCaseShort: "c"})
	f := cmd.Flags()
	f.StringVarP(&newName, "new-name", "N", "", "The new name for the box.")
	_ = cmd.MarkFlagRequired("new-name")
	f.StringVarP(&scope, "scope", "s", string(enums.RenameBoth), "Where to rename: local, remote, or both.")
	f.BoolVar(&refreshUserSymlinks, "refresh-user-symlinks", true, "Refresh the user symlinks.")
	f.BoolVar(&noRefreshSymlinks, "no-refresh-user-symlinks", false, "Do not refresh the user symlinks.")
	return cmd
}

func newSyncNameCommand() *cobra.Command {
	var (
		sel                 boxSelectorFlags
		toLocal, toRemote   bool
		refreshUserSymlinks bool
		noRefreshSymlinks   bool
	)

	cmd := &cobra.Command{
		Use:   "sync-name",
		Short: "Sync the box name between local and remote",
		RunE: func(cmd *cobra.Command, args []string) error {
			if noRefreshSymlinks {
				refreshUserSymlinks = false
			}
			// Exactly one direction. Neither is not a default, because there is
			// no safe guess about which side's name wins.
			if toLocal == toRemote {
				fmt.Fprintln(os.Stderr, "Error: Must specify exactly one of --to-local or --to-remote.")
				os.Exit(1)
			}
			direction := enums.NameToRemote
			if toLocal {
				direction = enums.NameToLocal
			}

			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{Label: "box"})
			if err != nil {
				return handleResolveError(err)
			}
			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			ctx, stop := maybeSoftInterrupt(true)
			defer stop()
			result, err := cmds.SyncName(ctx, cfg, store, cmds.SyncNameOptions{
				BoxIndexName: indexName, Direction: direction, Verbose: true, Out: os.Stdout,
			})
			if err != nil {
				return err
			}
			fmt.Printf("Result: %s\n", result)
			return maybeRefreshSymlinks(cfg, refreshUserSymlinks)
		},
	}
	sel.register(cmd, selectorSpec{Noun: "sync", MatchCaseShort: "c"})
	f := cmd.Flags()
	f.BoolVar(&toLocal, "to-local", false, "Sync name from remote to local.")
	f.BoolVar(&toRemote, "to-remote", false, "Sync name from local to remote.")
	f.BoolVar(&refreshUserSymlinks, "refresh-user-symlinks", true, "Refresh the user symlinks.")
	f.BoolVar(&noRefreshSymlinks, "no-refresh-user-symlinks", false, "Do not refresh the user symlinks.")
	return cmd
}

func newCopyCommand() *cobra.Command {
	var (
		sel                        boxSelectorFlags
		dest                       string
		copyMeta, copyConf         bool
		overwrite, showRcloneProgr bool
	)

	cmd := &cobra.Command{
		Use:   "copy",
		Short: "Copy a box's remote contents to a directory outside the yard",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{Label: "box"})
			if err != nil {
				return handleResolveError(err)
			}
			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			ctx, stop := maybeSoftInterrupt(true)
			defer stop()
			result, err := cmds.CopyFromRemote(ctx, cfg, store, perms.Adapter{},
				cmds.CopyFromRemoteOptions{
					BoxIndexName: indexName, DestPath: dest,
					CopyMeta: copyMeta, CopyConf: copyConf, Overwrite: overwrite,
					ShowRcloneProgress: showRcloneProgr, Verbose: true, Out: os.Stdout,
				})
			if err != nil {
				return err
			}
			fmt.Println(result)
			return nil
		},
	}
	// `copy` and `force-push` have NO short flag on --name-match-case: they
	// spend the letters elsewhere (-d for --dest, -f for --force).
	sel.register(cmd, selectorSpec{Noun: "copy"})
	f := cmd.Flags()
	f.StringVarP(&dest, "dest", "d", "", "Destination path for the copy.")
	_ = cmd.MarkFlagRequired("dest")
	f.BoolVar(&copyMeta, "meta", false, "Also copy boxmeta.toml.")
	f.BoolVar(&copyConf, "conf", false, "Also copy conf/ folder.")
	f.BoolVar(&overwrite, "overwrite", false, "Overwrite if dest exists.")
	f.BoolVar(&showRcloneProgr, "progress", false, "Show the progress of the copy in rclone.")
	return cmd
}

func newForcePushCommand() *cobra.Command {
	var (
		sel                boxSelectorFlags
		sourcePath         string
		force              bool
		progress           bool
		softInterruption   bool
		noSoftInterruption bool
	)

	cmd := &cobra.Command{
		Use:   "force-push",
		Short: "Force push a local folder to a box's remote DATA location",
		RunE: func(cmd *cobra.Command, args []string) error {
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
			indexName, err := sel.resolve(cfg, meta.BoxMetas, boxref.Options{Label: "box"})
			if err != nil {
				return handleResolveError(err)
			}
			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			ctx, stop := maybeSoftInterrupt(softInterruption)
			defer stop()
			return cmds.ForcePushToRemote(ctx, cfg, store, perms.Adapter{}, cmds.ForcePushOptions{
				BoxIndexName: indexName, SourcePath: sourcePath, Force: force,
				ShowRcloneProgress: progress, Verbose: true, Out: os.Stdout,
			})
		},
	}
	sel.register(cmd, selectorSpec{Noun: "push to"})
	f := cmd.Flags()
	f.StringVarP(&sourcePath, "source", "s", "", "Source folder to push.")
	_ = cmd.MarkFlagRequired("source")
	f.BoolVarP(&force, "force", "f", false, "Required: confirm force overwrite.")
	f.BoolVar(&progress, "progress", false, "Show the progress of the push in rclone.")
	f.BoolVar(&softInterruption, "soft-interruption-enabled", true, "Enable soft interruption.")
	f.BoolVar(&noSoftInterruption, "no-soft-interruption-enabled", false, "Disable soft interruption.")
	return cmd
}

func joinStrings(xs []string) string {
	out := ""
	for i, x := range xs {
		if i > 0 {
			out += ", "
		}
		out += x
	}
	return out
}

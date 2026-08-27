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

// metaEditFlags are the tail flags the four boxmeta-editing commands share.
type metaEditFlags struct {
	SyncAfter           bool
	SyncSetting         string
	RefreshUserSymlinks bool
	NoRefreshSymlinks   bool
	SoftInterruption    bool
	NoSoftInterruption  bool
}

func (f *metaEditFlags) register(cmd *cobra.Command) {
	fs := cmd.Flags()
	fs.BoolVarP(&f.SyncAfter, "sync-after", "s", false, "Sync the box after modifying it.")
	enumVar(fs, &f.SyncSetting, "sync-setting", "", string(enums.SyncCareful), "The sync setting to use.", enums.SyncSettingNames)
	fs.BoolVar(&f.RefreshUserSymlinks, "refresh-user-symlinks", true, "Refresh the user symlinks.")
	fs.BoolVar(&f.NoRefreshSymlinks, "no-refresh-user-symlinks", false, "Do not refresh the user symlinks.")
	fs.BoolVar(&f.SoftInterruption, "soft-interruption-enabled", true, "Enable soft interruption.")
	fs.BoolVar(&f.NoSoftInterruption, "no-soft-interruption-enabled", false, "Disable soft interruption.")
}

// after runs the optional META push and symlink refresh that follow a boxmeta
// edit. Only META is pushed: the edit touched nothing else, and pushing DATA
// here would turn a metadata change into a data transfer.
func (f *metaEditFlags) after(cfg *config.Config, indexName string, changed bool) error {
	if f.NoRefreshSymlinks {
		f.RefreshUserSymlinks = false
	}
	if f.NoSoftInterruption {
		f.SoftInterruption = false
	}
	setting := enums.SyncSetting(f.SyncSetting) // validated at parse time

	if changed && f.SyncAfter {
		client, err := rclone.New(cfg.RcloneConfigPath())
		if err != nil {
			return err
		}
		ctx := context.Background()
		if f.SoftInterruption {
			var stop func()
			ctx, stop = softInterrupt(ctx)
			defer stop()
		}
		push := enums.DirectionPush
		if _, err := cmds.SyncBox(ctx, cfg, storage.New(client), perms.Adapter{}, cmds.SyncBoxOptions{
			BoxIndexName: indexName,
			Direction:    &push,
			Setting:      setting,
			Choices:      []enums.BoxPart{enums.PartMeta},
			Verbose:      true,
			Out:          os.Stdout,
		}); err != nil {
			return err
		}
	}

	if f.RefreshUserSymlinks {
		meta, err := models.GetBoxyardMeta(cfg, false)
		if err != nil {
			return err
		}
		return symlinks.Build(cfg, meta)
	}
	return nil
}

// resolveTargetBox is the addressing these four commands share.
//
// They differ from `sync` and `path` in one way, which predates the general cwd
// rule and is kept: with NO selector they require the cwd to be inside a box
// and ERROR if it is not, rather than falling through to a picker. Offering a
// picker of 586 boxes to a bare `add-to-group backend` would be an invitation
// to edit the wrong one.
func resolveTargetBox(cfg *config.Config, sel *boxSelectorFlags) (string, error) {
	if sel.BoxPath == "" && sel.BoxIndexName == "" && sel.BoxID == "" && sel.BoxName == "" {
		cwd, err := os.Getwd()
		if err != nil {
			return "", err
		}
		indexName, err := models.IndexNameFromSubPath(cfg, cwd)
		if err != nil {
			return "", err
		}
		if indexName == "" {
			fmt.Fprintf(os.Stderr, "Box not found in `%s`.\n", cwd)
			os.Exit(1)
		}
		return indexName, nil
	}
	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return "", err
	}
	return sel.resolve(cfg, meta.BoxMetas, boxref.Options{AllowNoArgs: true})
}

func newAddToGroupCommand() *cobra.Command {
	var sel boxSelectorFlags
	var tail metaEditFlags

	cmd := &cobra.Command{
		Use:   "add-to-group GROUP...",
		Short: "Add a box to one or more groups",
		Args:  cobra.MinimumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, bm, err := loadTarget(&sel)
			if err != nil {
				return err
			}
			groups := append([]string{}, bm.Groups...)
			var added []string
			for _, g := range args {
				if containsString(groups, g) {
					fmt.Printf("Box `%s` already in group `%s`.\n", bm.IndexName(), g)
					continue
				}
				groups = append(groups, g)
				added = append(added, g)
			}
			if len(added) > 0 {
				if _, err := cmds.ModifyBoxMeta(cfg, bm.IndexName(), cmds.BoxMetaModifications{
					Groups: &groups,
				}); err != nil {
					return err
				}
			}
			return tail.after(cfg, bm.IndexName(), len(added) > 0)
		},
	}
	sel.register(cmd, selectorSpec{Noun: "modify", WithBoxPath: true, MatchCaseShort: "c"})
	tail.register(cmd)
	return cmd
}

func newRemoveFromGroupCommand() *cobra.Command {
	var sel boxSelectorFlags
	var tail metaEditFlags

	cmd := &cobra.Command{
		Use:   "remove-from-group GROUP...",
		Short: "Remove a box from one or more groups",
		Args:  cobra.MinimumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, bm, err := loadTarget(&sel)
			if err != nil {
				return err
			}
			groups := append([]string{}, bm.Groups...)
			var removed []string
			for _, g := range args {
				if !containsString(groups, g) {
					fmt.Printf("Box `%s` not in group `%s`.\n", bm.IndexName(), g)
					continue
				}
				groups = removeString(groups, g)
				removed = append(removed, g)
			}
			if len(removed) > 0 {
				if _, err := cmds.ModifyBoxMeta(cfg, bm.IndexName(), cmds.BoxMetaModifications{
					Groups: &groups,
				}); err != nil {
					return err
				}
			}
			return tail.after(cfg, bm.IndexName(), len(removed) > 0)
		},
	}
	sel.register(cmd, selectorSpec{Noun: "modify", WithBoxPath: true, MatchCaseShort: "c"})
	tail.register(cmd)
	return cmd
}

// parentSelector is the second set of selectors add-parent/remove-parent take.
type parentSelector struct {
	IndexName string
	ID        string
	Name      string
}

func (p *parentSelector) register(cmd *cobra.Command) {
	fs := cmd.Flags()
	fs.StringVar(&p.IndexName, "parent", "", "The index name of the parent box.")
	fs.StringVar(&p.ID, "parent-id", "", "The id of the parent box.")
	fs.StringVar(&p.Name, "parent-name", "", "The name of the parent box.")
}

func newAddParentCommand() *cobra.Command {
	var sel boxSelectorFlags
	var parent parentSelector
	var tail metaEditFlags

	cmd := &cobra.Command{
		Use:   "add-parent",
		Short: "Add a parent to a box",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, bm, err := loadTarget(&sel)
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			parentIndex, err := boxref.Resolve(meta.BoxMetas, boxref.FZF{}, boxref.Options{
				BoxIndexName: parent.IndexName, BoxID: parent.ID, BoxName: parent.Name,
				MatchMode: boxref.MatchMode(sel.MatchMode), MatchCase: sel.MatchCase,
				AllowNoArgs: false, Label: "parent",
			})
			if err != nil {
				return handleResolveError(err)
			}
			parentMeta, ok := meta.ByIndexName()[parentIndex]
			if !ok {
				fmt.Fprintf(os.Stderr, "Parent box `%s` not found.\n", parentIndex)
				os.Exit(1)
			}
			if containsString(bm.Parents, parentMeta.BoxID()) {
				fmt.Printf("Box `%s` already has parent `%s`.\n", bm.IndexName(), parentIndex)
				return tail.after(cfg, bm.IndexName(), false)
			}
			parents := append(append([]string{}, bm.Parents...), parentMeta.BoxID())
			if _, err := cmds.ModifyBoxMeta(cfg, bm.IndexName(), cmds.BoxMetaModifications{
				Parents: &parents,
			}); err != nil {
				return err
			}
			fmt.Printf("Added parent `%s` to box `%s`.\n", parentIndex, bm.IndexName())
			return tail.after(cfg, bm.IndexName(), true)
		},
	}
	sel.register(cmd, selectorSpec{Noun: "modify", WithBoxPath: true, MatchCaseShort: "c"})
	parent.register(cmd)
	tail.register(cmd)
	return cmd
}

func newRemoveParentCommand() *cobra.Command {
	var sel boxSelectorFlags
	var parent parentSelector
	var tail metaEditFlags

	cmd := &cobra.Command{
		Use:   "remove-parent",
		Short: "Remove a parent from a box",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, bm, err := loadTarget(&sel)
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}

			// An explicit --parent-id is used DIRECTLY and does not require the
			// parent box to still exist. That is the only way to drop a
			// dangling parent left behind by a deleted box — which is exactly
			// what doctor's `tree-orphans` check points here for.
			targetID, label := parent.ID, parent.ID
			if parent.ID == "" {
				parentIndex, err := boxref.Resolve(meta.BoxMetas, boxref.FZF{}, boxref.Options{
					BoxIndexName: parent.IndexName, BoxName: parent.Name,
					MatchMode: boxref.MatchMode(sel.MatchMode), MatchCase: sel.MatchCase,
					AllowNoArgs: false, Label: "parent",
				})
				if err != nil {
					return handleResolveError(err)
				}
				label = parentIndex
				if pm, ok := meta.ByIndexName()[parentIndex]; ok {
					targetID = pm.BoxID()
				}
			}
			if targetID == "" || !containsString(bm.Parents, targetID) {
				// Exit 1, unlike add-parent's already-has case, which exits 0.
				// The asymmetry is the Python's, and it is the contract: asking
				// to remove something that is not there is treated as a
				// mistake, asking to add something already there is not.
				fmt.Fprintf(os.Stderr, "Box `%s` does not have parent `%s`.\n", bm.IndexName(), label)
				os.Exit(1)
			}
			parents := removeString(append([]string{}, bm.Parents...), targetID)
			if _, err := cmds.ModifyBoxMeta(cfg, bm.IndexName(), cmds.BoxMetaModifications{
				Parents: &parents,
			}); err != nil {
				return err
			}
			fmt.Printf("Removed parent `%s` from box `%s`.\n", label, bm.IndexName())
			return tail.after(cfg, bm.IndexName(), true)
		},
	}
	sel.register(cmd, selectorSpec{Noun: "modify", WithBoxPath: true, MatchCaseShort: "c"})
	parent.register(cmd)
	tail.register(cmd)
	return cmd
}

func newCreateUserSymlinksCommand() *cobra.Command {
	var userBoxesPath, userBoxGroupsPath string

	cmd := &cobra.Command{
		Use:   "create-user-symlinks",
		Short: "Rebuild the user box group symlink tree",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			if userBoxesPath != "" {
				if cfg.UserBoxesPath, err = config.ExpandUser(userBoxesPath); err != nil {
					return err
				}
			}
			if userBoxGroupsPath != "" {
				if cfg.UserBoxGroupsPath, err = config.ExpandUser(userBoxGroupsPath); err != nil {
					return err
				}
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			return symlinks.Build(cfg, meta)
		},
	}
	cmd.Flags().StringVarP(&userBoxesPath, "user-boxes-path", "u", "", "The path to the user boxes directory.")
	cmd.Flags().StringVarP(&userBoxGroupsPath, "user-box-groups-path", "g", "", "The path to the user box groups directory.")
	return cmd
}

// loadTarget resolves the config and the box the command acts on.
func loadTarget(sel *boxSelectorFlags) (*config.Config, *models.BoxMeta, error) {
	cfg, err := appState.Config()
	if err != nil {
		return nil, nil, err
	}
	indexName, err := resolveTargetBox(cfg, sel)
	if err != nil {
		return nil, nil, handleResolveError(err)
	}
	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return nil, nil, err
	}
	bm, ok := meta.ByIndexName()[indexName]
	if !ok {
		fmt.Fprintf(os.Stderr, "Box with index name `%s` not found.\n", indexName)
		os.Exit(1)
	}
	return cfg, bm, nil
}

func containsString(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

func removeString(xs []string, drop string) []string {
	out := xs[:0]
	for _, x := range xs {
		if x != drop {
			out = append(out, x)
		}
	}
	return out
}

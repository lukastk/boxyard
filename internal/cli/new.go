package cli

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/boxref"
	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/naming"
	"github.com/lukastk/boxyard/internal/symlinks"
	"github.com/spf13/cobra"
)

func newNewCommand() *cobra.Command {
	var (
		storageLocation      string
		boxName              string
		fromPath             string
		copyFromPath         bool
		gitCloneURL          string
		creatorHostname      string
		creationTimestampUTC string
		groups               []string
		parent               string
		initialiseGit        bool
		noInitialiseGit      bool
		claim                bool
		noClaim              bool
		refreshUserSymlinks  bool
		noRefreshSymlinks    bool
	)

	cmd := &cobra.Command{
		Use:   "new",
		Short: "Create a new box",
		RunE: func(cmd *cobra.Command, args []string) error {
			if noInitialiseGit {
				initialiseGit = false
			}
			if noClaim {
				claim = false
			}
			if noRefreshSymlinks {
				refreshUserSymlinks = false
			}

			cfg, err := appState.Config()
			if err != nil {
				return err
			}

			// The CLI derives the name itself so that "No box name provided."
			// is reported here, before anything is created — `new_box` would
			// raise its own (differently worded) error otherwise.
			if boxName == "" && fromPath != "" {
				boxName = filepath.Base(fromPath)
			}
			if boxName == "" && gitCloneURL != "" {
				boxName = cmds.ExtractBoxNameFromGitURL(gitCloneURL)
			}
			if boxName == "" {
				fmt.Fprintln(os.Stderr, "No box name provided.")
				os.Exit(1)
			}

			var timestamp *time.Time
			if creationTimestampUTC != "" {
				parsed, err := parseCreationTimestamp(creationTimestampUTC)
				if err != nil {
					fmt.Fprintf(os.Stderr, "Invalid creation timestamp: %s\n", creationTimestampUTC)
					os.Exit(1)
				}
				timestamp = &parsed
			}

			// Validate --group and --parent BEFORE creating anything.
			//
			// These used to be applied AFTER NewBox returned, outside its
			// rollback, so a bad group name or a missing parent exited 1 with
			// the box already fully created and registered — and a caller
			// treating a non-zero exit as "nothing happened" accumulated
			// orphans. Both are knowable up front, so validating here is
			// cheaper than extending the rollback, and it stops the index name
			// being printed for a box the command then fails on.
			for _, g := range groups {
				if err := naming.ValidateGroupName(g); err != nil {
					return err
				}
			}
			parentID := ""
			if parent != "" {
				parentID, err = resolveParentBoxID(cfg, parent)
				if err != nil {
					fmt.Fprintln(os.Stderr, err)
					os.Exit(1)
				}
			}

			// A store is only needed when `sync_before_new_box` is on, but
			// building it is cheap and unconditional here keeps the failure
			// (a bad rclone config) reported before anything is created.
			store, err := newStore(cfg)
			if err != nil {
				return err
			}
			indexName, err := cmds.NewBox(context.Background(), cfg, store, cmds.NewBoxOptions{
				StorageLocation:      storageLocation,
				BoxName:              boxName,
				FromPath:             fromPath,
				CopyFromPath:         copyFromPath,
				CreatorHostname:      creatorHostname,
				CreationTimestampUTC: timestamp,
				InitialiseGit:        initialiseGit,
				GitCloneURL:          gitCloneURL,
				Claim:                &claim,
				Verbose:              false,
			})
			if err != nil {
				return err
			}
			fmt.Println(indexName)

			// Applied after creation still: modify_boxmeta is what enforces
			// the rules that need the box to exist (virtual groups, unique
			// names, cycles, single_parent). What moved above is only the
			// validation that does NOT.
			if len(groups) > 0 {
				merged := append(append([]string{}, cfg.DefaultBoxGroups...), groups...)
				if _, err := cmds.ModifyBoxMeta(cfg, indexName, cmds.BoxMetaModifications{
					Groups: &merged,
				}); err != nil {
					return err
				}
			}

			if parentID != "" {
				if _, err := cmds.ModifyBoxMeta(cfg, indexName, cmds.BoxMetaModifications{
					Parents: &[]string{parentID},
				}); err != nil {
					return err
				}
			}

			if refreshUserSymlinks {
				meta, err := models.GetBoxyardMeta(cfg, false)
				if err != nil {
					return err
				}
				return symlinks.Build(cfg, meta)
			}
			return nil
		},
	}

	f := cmd.Flags()
	f.StringVarP(&storageLocation, "storage-location", "s", "", "The storage location to create the new box in.")
	f.StringVarP(&boxName, "box-name", "n", "", "The name of the box, the id or the path of the box.")
	f.StringVarP(&fromPath, "from", "f", "", "Path to a local directory to move into boxyard as a new box.")
	f.BoolVarP(&copyFromPath, "copy", "c", false, "Copy the contents of the from_path into the new box.")
	f.StringVar(&gitCloneURL, "git-clone", "", "Git URL (SSH or HTTPS) to clone as the new box.")
	f.StringVar(&creatorHostname, "creator-hostname", "", "Used to explicitly set the creator hostname of the new box.")
	f.StringVar(&creationTimestampUTC, "creation-timestamp-utc", "", "The timestamp of the new box. Should be in the form '%Y%m%d_%H%M%S' (e.g. '20251116_105532') or '%Y%m%d' (e.g. '20251116'). If not provided, the current UTC timestamp will be used.")
	f.StringArrayVarP(&groups, "group", "g", nil, "The groups to add the new box to.")
	f.StringVar(&parent, "parent", "", "Parent box (index name, id, or name) to set for the new box.")
	// typer renders a bool option with a True default as a --x/--no-x pair, and
	// the negative form is what myrig's helpers pass, so both are registered.
	f.BoolVar(&initialiseGit, "initialise-git", true, "Initialise a git box in the new box.")
	f.BoolVar(&noInitialiseGit, "no-initialise-git", false, "Do not initialise a git box in the new box.")
	f.BoolVar(&claim, "claim", true, "Make this machine the box's write owner.")
	f.BoolVar(&noClaim, "no-claim", false, "Create the box without claiming it, for one that will be worked on elsewhere.")
	f.BoolVar(&refreshUserSymlinks, "refresh-user-symlinks", true, "Refresh the user symlinks.")
	f.BoolVar(&noRefreshSymlinks, "no-refresh-user-symlinks", false, "Do not refresh the user symlinks.")
	return cmd
}

// parseCreationTimestamp accepts the two formats the Python CLI accepts, in the
// order it tries them.
func parseCreationTimestamp(s string) (time.Time, error) {
	if t, err := time.Parse(boxconst.BoxTimestampFormat, s); err == nil {
		return t.UTC(), nil
	}
	t, err := time.Parse(boxconst.BoxTimestampFormatDateOnly, s)
	if err != nil {
		return time.Time{}, err
	}
	return t.UTC(), nil
}

// resolveParentBoxID accepts the three forms `--parent` documents: an index
// name, a box id, or a name.
//
// The two EXACT forms are tried first, then the name match. Python honoured
// only the name until v0.5.8 — the value went in as a name, and an index name
// is never a substring of the bare name it ends with, so `--parent
// 20260601_ab12cd__thing` reported "not found". The order is unambiguous
// because the three have distinct shapes and the first two are matched by
// equality.
func resolveParentBoxID(cfg *config.Config, parent string) (string, error) {
	meta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return "", err
	}
	if bm, ok := meta.ByIndexName()[parent]; ok {
		return bm.BoxID(), nil
	}
	if bm, ok := meta.ByBoxID()[parent]; ok {
		return bm.BoxID(), nil
	}
	indexName, err := boxref.Resolve(meta.BoxMetas, boxref.FZF{}, boxref.Options{
		BoxName: parent, Label: "parent",
	})
	if err != nil {
		return "", fmt.Errorf("Parent box '%s' not found.", parent)
	}
	bm, ok := meta.ByIndexName()[indexName]
	if !ok {
		return "", fmt.Errorf("Parent box '%s' not found.", parent)
	}
	return bm.BoxID(), nil
}

package cli

import (
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/cmds"
	"github.com/lukastk/boxyard/internal/models"
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
		refreshUserSymlinks  bool
		noRefreshSymlinks    bool
	)

	cmd := &cobra.Command{
		Use:   "new",
		Short: "Create a new box",
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(groups) > 0 {
				return notPorted("`--group`")
			}
			if parent != "" {
				return notPorted("`--parent`")
			}
			if noInitialiseGit {
				initialiseGit = false
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
				fmt.Println("No box name provided.")
				os.Exit(1)
			}

			var timestamp *time.Time
			if creationTimestampUTC != "" {
				parsed, err := parseCreationTimestamp(creationTimestampUTC)
				if err != nil {
					fmt.Printf("Invalid creation timestamp: %s\n", creationTimestampUTC)
					os.Exit(1)
				}
				timestamp = &parsed
			}

			indexName, err := cmds.NewBox(cfg, cmds.NewBoxOptions{
				StorageLocation:      storageLocation,
				BoxName:              boxName,
				FromPath:             fromPath,
				CopyFromPath:         copyFromPath,
				CreatorHostname:      creatorHostname,
				CreationTimestampUTC: timestamp,
				InitialiseGit:        initialiseGit,
				GitCloneURL:          gitCloneURL,
				Verbose:              false,
			})
			if err != nil {
				return err
			}
			fmt.Println(indexName)

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

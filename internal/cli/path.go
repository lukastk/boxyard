package cli

import (
	"fmt"
	"os"

	"github.com/lukastk/boxyard/internal/boxref"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/spf13/cobra"
)

func newPathCommand() *cobra.Command {
	var (
		sel           boxSelectorFlags
		pickFirst     bool
		pathOption    string
		includeGroups []string
		excludeGroups []string
		allBoxes      bool
		groupFilter   string
	)

	cmd := &cobra.Command{
		Use:   "path",
		Short: "Get the path of a box",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}

			candidates, err := filterByGroups(meta.BoxMetas, includeGroups, excludeGroups, groupFilter)
			if err != nil {
				return err
			}
			if !allBoxes {
				// The default is the boxes actually checked out here: `path` is
				// used to `cd` into one, and a path to a box that is not on this
				// machine is not a path anyone can use.
				included := candidates[:0]
				for _, bm := range candidates {
					if bm.CheckIncluded(cfg) {
						included = append(included, bm)
					}
				}
				candidates = included
			}

			indexName, err := sel.resolve(cfg, candidates, boxref.Options{
				AllowNoArgs: true,
				PickFirst:   pickFirst,
			})
			if err != nil {
				return handleResolveError(err)
			}
			bm, ok := meta.ByIndexName()[indexName]
			if !ok {
				fmt.Printf("Box with index name `%s` not found.\n", indexName)
				os.Exit(1)
			}

			out, err := boxPathFor(cfg, bm, pathOption)
			if err != nil {
				return err
			}
			fmt.Println(out)
			return nil
		},
	}

	// `path` has no --box-path: it PRODUCES a path rather than taking one.
	sel.register(cmd, selectorSpec{
		Noun: "show", MatchCaseShort: "c", BoxNameHelp: "What box path to show.",
	})
	f := cmd.Flags()
	f.BoolVarP(&pickFirst, "pick-first", "1", false, "Pick the first box if there are multiple matches.")
	enumVar(f, &pathOption, "path-option", "p", "data", "The part of the box to show the path of.", pathOptionNames)
	f.StringArrayVarP(&includeGroups, "include-group", "g", nil, "The group to include in the search.")
	f.StringArrayVarP(&excludeGroups, "exclude-group", "e", nil, "The group to exclude from the search.")
	f.BoolVarP(&allBoxes, "all", "a", false, "Show all boxes, including those not included locally.")
	f.StringVarP(&groupFilter, "group-filter", "f", "", "The filter to apply to the groups.")
	return cmd
}

// pathOptionNames is what --path-option accepts, in the order the Python's
// Literal declares them. boxPathFor below must handle every one.
var pathOptionNames = []string{
	"data", "meta", "conf", "root",
	"sync-record-data", "sync-record-meta", "sync-record-conf",
}

// boxPathFor resolves --path-option to a path. The names are a documented
// contract: myrig's shell functions pass them straight through.
func boxPathFor(cfg *config.Config, bm *models.BoxMeta, option string) (string, error) {
	switch option {
	case "data":
		return bm.LocalPartPath(cfg, enums.PartData)
	case "meta":
		return bm.LocalPartPath(cfg, enums.PartMeta)
	case "conf":
		return bm.LocalPartPath(cfg, enums.PartConf)
	case "root":
		return bm.LocalPath(cfg), nil
	case "sync-record-data":
		return bm.LocalSyncRecordPath(cfg, enums.PartData), nil
	case "sync-record-meta":
		return bm.LocalSyncRecordPath(cfg, enums.PartMeta), nil
	case "sync-record-conf":
		return bm.LocalSyncRecordPath(cfg, enums.PartConf), nil
	default:
		return "", fmt.Errorf("invalid path option: %s", option)
	}
}

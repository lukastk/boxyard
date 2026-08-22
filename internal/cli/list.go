package cli

import (
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/groupexpr"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/strict"
	"github.com/spf13/cobra"
)

// notPorted reports a flag that the Go implementation does not yet handle.
//
// Failing loudly is deliberate. A command that silently ignores a flag it does
// not understand diverges from the Python without anyone noticing, which is
// precisely what the parity suite exists to catch — so the unported paths
// refuse rather than approximate.
func notPorted(what string) error {
	return fmt.Errorf("%s is not yet ported to the Go implementation; use the Python boxyard for it", what)
}

// filterByGroups applies --include-group, --exclude-group and --group-filter,
// in that order, matching _get_filtered_box_metas.
func filterByGroups(metas []*models.BoxMeta, include, exclude []string, filterExpr string) ([]*models.BoxMeta, error) {
	hasAny := func(groups, wanted []string) bool {
		for _, g := range groups {
			for _, w := range wanted {
				if g == w {
					return true
				}
			}
		}
		return false
	}

	if len(include) > 0 {
		var out []*models.BoxMeta
		for _, bm := range metas {
			if hasAny(bm.Groups, include) {
				out = append(out, bm)
			}
		}
		metas = out
	}
	if len(exclude) > 0 {
		var out []*models.BoxMeta
		for _, bm := range metas {
			if !hasAny(bm.Groups, exclude) {
				out = append(out, bm)
			}
		}
		metas = out
	}
	if filterExpr != "" {
		pred, err := groupexpr.Parse(filterExpr)
		if err != nil {
			return nil, err
		}
		var out []*models.BoxMeta
		for _, bm := range metas {
			if pred(bm.Groups) {
				out = append(out, bm)
			}
		}
		metas = out
	}
	return metas, nil
}

func statusMarker(cfg *config.Config, bm *models.BoxMeta, show bool) string {
	if !show {
		return ""
	}
	if bm.CheckIncluded(cfg) {
		return "● "
	}
	return "○ "
}

func newListCommand() *cobra.Command {
	var (
		storageLocations []string
		outputFormat     string
		includeGroups    []string
		excludeGroups    []string
		groupFilter      string
		view             string
		showStatus       bool
		childrenOf       string
		descendantsOf    string
		parentOf         string
		ancestorsOf      string
		rootsOnly        bool
		leavesOnly       bool
		hideGroups       []string
	)

	cmd := &cobra.Command{
		Use:   "list",
		Short: "List all boxes in the yard",
		RunE: func(cmd *cobra.Command, args []string) error {
			for flag, set := range map[string]bool{
				"--children-of":    childrenOf != "",
				"--descendants-of": descendantsOf != "",
				"--parent-of":      parentOf != "",
				"--ancestors-of":   ancestorsOf != "",
				"--roots":          rootsOnly,
				"--leaves":         leavesOnly,
				"--hide-group":     len(hideGroups) > 0,
			} {
				if set {
					return notPorted(flag)
				}
			}
			if view != "flat" {
				return notPorted("--view " + view)
			}
			if outputFormat != "text" && outputFormat != "json" {
				return fmt.Errorf("invalid output format %q (want text or json)", outputFormat)
			}

			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			if len(storageLocations) == 0 {
				for name := range cfg.StorageLocations {
					storageLocations = append(storageLocations, name)
				}
				sort.Strings(storageLocations)
			}
			for _, sl := range storageLocations {
				if _, ok := cfg.StorageLocations[sl]; !ok {
					fmt.Printf("Invalid storage location: %v\n", storageLocations)
					os.Exit(1)
				}
			}

			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}
			wanted := map[string]bool{}
			for _, sl := range storageLocations {
				wanted[sl] = true
			}
			var metas []*models.BoxMeta
			for _, bm := range meta.BoxMetas {
				if wanted[bm.StorageLocation] {
					metas = append(metas, bm)
				}
			}
			if metas, err = filterByGroups(metas, includeGroups, excludeGroups, groupFilter); err != nil {
				return err
			}

			if outputFormat == "json" {
				// A list of full BoxMeta objects, exactly as
				// json.dumps([rm.model_dump() ...], indent=2) produces. myrig's
				// box picker and the sesh plugin both parse this.
				if metas == nil {
					metas = []*models.BoxMeta{}
				}
				for _, bm := range metas {
					bm.NormalizeSlices()
				}
				out, err := strict.MarshalJSONIndent(metas)
				if err != nil {
					return err
				}
				fmt.Println(string(out))
				return nil
			}

			var b strings.Builder
			for _, bm := range metas {
				b.WriteString(statusMarker(cfg, bm, showStatus))
				b.WriteString(bm.IndexName())
				b.WriteString("\n")
			}
			fmt.Print(b.String())
			return nil
		},
	}

	f := cmd.Flags()
	f.StringArrayVarP(&storageLocations, "storage-location", "s", nil,
		"The storage location to get the status of. If not provided, the status of all storage locations will be shown.")
	f.StringVarP(&outputFormat, "output-format", "o", "text", "The format of the output.")
	f.StringArrayVarP(&includeGroups, "include-group", "g", nil, "The group to include in the output.")
	f.StringArrayVarP(&excludeGroups, "exclude-group", "e", nil, "The group to exclude from the output.")
	f.StringVarP(&groupFilter, "group-filter", "f", "", "The filter to apply to the groups.")
	f.StringVarP(&view, "view", "v", "flat", "Display mode: flat list, parent-child tree, or grouped by group.")
	f.BoolVar(&showStatus, "show-status", false, "Show included/excluded status icon (●/○) for each box.")
	f.StringVar(&childrenOf, "children-of", "", "Filter to direct children of the given box.")
	f.StringVar(&descendantsOf, "descendants-of", "", "Filter to all descendants of the given box.")
	f.StringVar(&parentOf, "parent-of", "", "Filter to direct parents of the given box.")
	f.StringVar(&ancestorsOf, "ancestors-of", "", "Filter to all ancestors of the given box.")
	f.BoolVar(&rootsOnly, "roots", false, "Only show root boxes (no parents).")
	f.BoolVar(&leavesOnly, "leaves", false, "Only show leaf boxes (no children).")
	f.StringArrayVar(&hideGroups, "hide-group", nil, "Hide a group branch in --view groups.")
	return cmd
}

func newListGroupsCommand() *cobra.Command {
	var (
		boxPath        string
		boxIndexName   string
		listAll        bool
		includeVirtual bool
	)

	cmd := &cobra.Command{
		Use:   "list-groups",
		Short: "List all groups a box belongs to, or all groups with --all",
		RunE: func(cmd *cobra.Command, args []string) error {
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}

			if boxIndexName != "" && boxPath != "" {
				fmt.Println("Both --box and --box-path cannot be provided.")
				os.Exit(1)
			}
			if listAll && (boxPath != "" || boxIndexName != "") {
				fmt.Println("Cannot provide both --box and --box-path when using --all.")
				os.Exit(1)
			}

			if listAll {
				groupConfigs, virtualConfigs := models.GroupConfigs(cfg, meta.BoxMetas)
				var groups []string
				for name := range groupConfigs {
					groups = append(groups, name)
				}
				if includeVirtual {
					for name := range virtualConfigs {
						groups = append(groups, name)
					}
				}
				sort.Strings(groups)
				for _, g := range groups {
					fmt.Println(g)
				}
				return nil
			}

			if boxIndexName == "" && boxPath == "" {
				if boxPath, err = os.Getwd(); err != nil {
					return err
				}
			}
			if boxPath != "" {
				if boxIndexName, err = models.IndexNameFromSubPath(cfg, boxPath); err != nil {
					return err
				}
				if boxIndexName == "" {
					fmt.Println("Could not determine the box index name from the provided box path.")
					os.Exit(1)
				}
			}

			bm, ok := meta.ByIndexName()[boxIndexName]
			if !ok {
				fmt.Printf("Box with index name `%s` not found.\n", boxIndexName)
				os.Exit(1)
			}
			groups := append([]string{}, bm.Groups...)
			sort.Strings(groups)
			for _, g := range groups {
				fmt.Println(g)
			}
			return nil
		},
	}

	f := cmd.Flags()
	f.StringVarP(&boxPath, "box-path", "p", "", "The path to the box to get the groups of.")
	f.StringVarP(&boxIndexName, "box", "r", "", "The box index name to get the groups of.")
	f.BoolVarP(&listAll, "all", "a", false, "List all groups, including virtual groups.")
	f.BoolVarP(&includeVirtual, "include-virtual", "v", false, "Include virtual groups in the output.")
	return cmd
}

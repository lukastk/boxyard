package cli

import (
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/richstyle"
	"github.com/lukastk/boxyard/internal/strict"
	"github.com/spf13/cobra"
)

func newTreeCommand() *cobra.Command {
	var (
		storageLocations []string
		includeGroups    []string
		excludeGroups    []string
		groupFilter      string
		rootBox          string
		outputFormat     string
		showStatus       bool
	)

	cmd := &cobra.Command{
		Use:   "tree",
		Short: "Show the box parent/child tree",
		RunE: func(cmd *cobra.Command, args []string) error {
			if outputFormat != "text" && outputFormat != "json" {
				return &usageError{err: fmt.Errorf("invalid output format: %q", outputFormat)}
			}
			cfg, err := appState.Config()
			if err != nil {
				return err
			}
			meta, err := models.GetBoxyardMeta(cfg, false)
			if err != nil {
				return err
			}

			boxes := meta.BoxMetas
			if len(storageLocations) > 0 {
				var kept []*models.BoxMeta
				for _, bm := range boxes {
					if containsString(storageLocations, bm.StorageLocation) {
						kept = append(kept, bm)
					}
				}
				boxes = kept
			}
			boxes, err = filterByGroups(boxes, includeGroups, excludeGroups, groupFilter)
			if err != nil {
				return err
			}
			filtered := &models.BoxyardMeta{BoxMetas: boxes}

			var chosen *models.BoxMeta
			if rootBox != "" {
				chosen, err = resolveTreeRoot(filtered, boxes, rootBox)
				if err != nil {
					fmt.Fprintln(os.Stderr, err)
					os.Exit(1)
				}
			}

			if outputFormat == "json" {
				// The JSON path walks the REGISTRY order, not a sorted one.
				// Python's `get_dag_nested` iterates the box list as it comes
				// off disk, and the text path sorts separately — the two
				// formats genuinely disagree, and reproducing that is the
				// point of a port.
				roots := chosen2roots(chosen, filtered.Roots(), boxes)
				fmt.Println(renderTreeJSON(filtered, roots))
				return nil
			}

			roots := filtered.Roots()
			sort.Slice(roots, func(i, j int) bool { return roots[i].IndexName() < roots[j].IndexName() })
			if chosen != nil {
				roots = []*models.BoxMeta{chosen}
			}
			renderTreeText(cfg, filtered, boxes, roots, showStatus)
			return nil
		},
	}

	f := cmd.Flags()
	f.StringArrayVarP(&storageLocations, "storage-location", "s", nil, "The storage location to show.")
	f.StringArrayVarP(&includeGroups, "include-group", "g", nil, "The group to include in the tree.")
	f.StringArrayVarP(&excludeGroups, "exclude-group", "e", nil, "The group to exclude from the tree.")
	f.StringVarP(&groupFilter, "group-filter", "f", "", "Boolean filter expression over groups.")
	f.StringVar(&rootBox, "root", "", "Show subtree from a specific box.")
	f.StringVarP(&outputFormat, "output-format", "o", "text", "The format of the output.")
	f.BoolVar(&showStatus, "show-status", false, "Show included/excluded status icons.")
	return cmd
}

// resolveTreeRoot accepts a box id, an index name, or a name substring —
// ambiguity is a loud error rather than a guess.
func resolveTreeRoot(filtered *models.BoxyardMeta, boxes []*models.BoxMeta, rootBox string) (*models.BoxMeta, error) {
	if bm, ok := filtered.ByBoxID()[rootBox]; ok {
		return bm, nil
	}
	if bm, ok := filtered.ByIndexName()[rootBox]; ok {
		return bm, nil
	}
	var matches []*models.BoxMeta
	for _, bm := range boxes {
		if strings.Contains(bm.Name, rootBox) {
			matches = append(matches, bm)
		}
	}
	switch len(matches) {
	case 1:
		return matches[0], nil
	case 0:
		return nil, fmt.Errorf("Root box '%s' not found.", rootBox)
	default:
		names := make([]string, len(matches))
		for i, m := range matches {
			names[i] = "'" + m.IndexName() + "'"
		}
		return nil, fmt.Errorf("Ambiguous root box '%s', matches: [%s]", rootBox, strings.Join(names, ", "))
	}
}

// renderTreeJSON reproduces `_fast.get_dag_nested`, whose objects are in
// INSERTION order. encoding/json sorts map keys, so the object is assembled by
// hand.
func renderTreeJSON(meta *models.BoxyardMeta, roots []*models.BoxMeta) string {
	visited := map[string]bool{}
	var b strings.Builder
	b.WriteString("{\n")
	first := true
	for _, root := range roots {
		body, ok := treeSubtree(meta, root.BoxID(), visited, 1)
		if !ok {
			continue
		}
		if !first {
			b.WriteString(",\n")
		}
		first = false
		fmt.Fprintf(&b, "  %q: %s", root.BoxID(), body)
	}
	if !first {
		b.WriteString("\n")
	}
	b.WriteString("}")
	return b.String()
}

func treeSubtree(meta *models.BoxyardMeta, boxID string, visited map[string]bool, depth int) (string, bool) {
	bm, ok := meta.ByBoxID()[boxID]
	if !ok || visited[boxID] {
		return "", false
	}
	visited[boxID] = true

	pad := strings.Repeat("  ", depth)
	inner := strings.Repeat("  ", depth+1)
	var b strings.Builder
	b.WriteString("{\n")
	fmt.Fprintf(&b, "%s%q: %s,\n", inner, "name", jsonString(bm.Name))
	fmt.Fprintf(&b, "%s%q: %s,\n", inner, "index_name", jsonString(bm.IndexName()))
	fmt.Fprintf(&b, "%s%q: %s,\n", inner, "box_id", jsonString(bm.BoxID()))
	fmt.Fprintf(&b, "%s%q: %s,\n", inner, "groups", jsonStringArray(bm.Groups, depth+1))

	children := meta.ChildrenOf(boxID)
	if len(children) == 0 {
		fmt.Fprintf(&b, "%s%q: {}\n", inner, "children")
	} else {
		fmt.Fprintf(&b, "%s%q: {\n", inner, "children")
		childPad := strings.Repeat("  ", depth+2)
		firstChild := true
		for _, child := range children {
			body, ok := treeSubtree(meta, child.BoxID(), visited, depth+2)
			if !ok {
				continue
			}
			if !firstChild {
				b.WriteString(",\n")
			}
			firstChild = false
			fmt.Fprintf(&b, "%s%q: %s", childPad, child.BoxID(), body)
		}
		if !firstChild {
			b.WriteString("\n")
		}
		fmt.Fprintf(&b, "%s}\n", inner)
	}
	fmt.Fprintf(&b, "%s}", pad)
	return b.String(), true
}

// renderTreeText draws the box tree.
//
// The label is plain text on purpose. Python built it as a rich MARKUP string,
// so `[groups: ...]` was parsed as a style tag and swallowed whole — `boxyard
// tree` never once printed a box's groups (fixed in Python v0.5.9). A box name
// containing `[` had the same problem.
func renderTreeText(cfg *config.Config, meta *models.BoxyardMeta, boxes []*models.BoxMeta,
	roots []*models.BoxMeta, showStatus bool) {

	label := func(bm *models.BoxMeta) string {
		status := ""
		if showStatus {
			if bm.CheckIncluded(cfg) {
				status = "● "
			} else {
				status = "○ "
			}
		}
		groups := ""
		if len(bm.Groups) > 0 {
			groups = " [groups: " + strings.Join(bm.Groups, ", ") + "]"
		}
		return fmt.Sprintf("%s%s (%s)%s", status, bm.Name, bm.BoxID(), groups)
	}

	// A rich Tree in the Python, so it wraps to the console width. Its labels
	// are plain rich Text — the `[groups: ...]` suffix was swallowed as markup
	// until v0.5.9 — so they are escaped and carry no styling.
	shown := map[string]bool{}
	var add func(node *richstyle.TreeNode, bm *models.BoxMeta)
	add = func(node *richstyle.TreeNode, bm *models.BoxMeta) {
		child := node.Add(richstyle.Escape(label(bm)))
		shown[bm.BoxID()] = true
		children := meta.ChildrenOf(bm.BoxID())
		sort.Slice(children, func(i, j int) bool { return children[i].IndexName() < children[j].IndexName() })
		for _, c := range children {
			add(child, c)
		}
	}

	root := &richstyle.TreeNode{Label: "boxyard"}
	for _, r := range roots {
		add(root, r)
	}
	// A box whose parent is outside the filtered set would otherwise vanish
	// from the tree entirely.
	if orphans := collectOrphans(meta, boxes, roots); len(orphans) > 0 {
		node := root.Add(richstyle.Escape("[unknown parent]"))
		for _, orphan := range orphans {
			add(node, orphan)
		}
	}
	printTree(root)
}

func hasOrphans(meta *models.BoxyardMeta, boxes, roots []*models.BoxMeta) bool {
	return len(collectOrphans(meta, boxes, roots)) > 0
}

func collectOrphans(meta *models.BoxyardMeta, boxes, roots []*models.BoxMeta) []*models.BoxMeta {
	shown := map[string]bool{}
	var mark func(boxID string)
	mark = func(boxID string) {
		if shown[boxID] {
			return
		}
		shown[boxID] = true
		for _, child := range meta.ChildrenOf(boxID) {
			mark(child.BoxID())
		}
	}
	for _, r := range roots {
		mark(r.BoxID())
	}
	var out []*models.BoxMeta
	for _, bm := range boxes {
		if !shown[bm.BoxID()] {
			out = append(out, bm)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].IndexName() < out[j].IndexName() })
	return out
}

// jsonString and jsonStringArray render values the way Python's
// json.dumps(indent=2) does: non-ASCII ESCAPED, which is json.dumps's default
// and differs from pydantic's raw UTF-8 (see internal/strict).
func jsonString(s string) string {
	out, err := strict.MarshalJSONIndent(s)
	if err != nil {
		// A string always marshals; a failure here would be a bug in the
		// encoder, not in the data.
		panic(err)
	}
	return string(out)
}

func jsonStringArray(xs []string, depth int) string {
	if len(xs) == 0 {
		return "[]"
	}
	pad := strings.Repeat("  ", depth+1)
	var b strings.Builder
	b.WriteString("[\n")
	for i, x := range xs {
		b.WriteString(pad + jsonString(x))
		if i < len(xs)-1 {
			b.WriteString(",")
		}
		b.WriteString("\n")
	}
	b.WriteString(strings.Repeat("  ", depth) + "]")
	return b.String()
}

// chosen2roots picks the JSON roots: the one box asked for, or every root in
// REGISTRY order (the order `boxyard_meta.json` lists them in), which is what
// Python's get_dag_nested walks.
func chosen2roots(chosen *models.BoxMeta, roots []*models.BoxMeta, boxes []*models.BoxMeta) []*models.BoxMeta {
	if chosen != nil {
		return []*models.BoxMeta{chosen}
	}
	isRoot := map[string]bool{}
	for _, r := range roots {
		isRoot[r.BoxID()] = true
	}
	var out []*models.BoxMeta
	for _, bm := range boxes {
		if isRoot[bm.BoxID()] {
			out = append(out, bm)
		}
	}
	return out
}

package cli

import (
	"fmt"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/richstyle"
)

// resolveListRef accepts a box id, an index name, or a name substring — the
// three forms `list`'s hierarchy filters take.
//
// An AMBIGUOUS name substring resolves to nothing rather than to the first
// match: the caller then reports "not found", which is the Python's behaviour
// and better than silently filtering by the wrong box.
func resolveListRef(meta *models.BoxyardMeta, ref string) *models.BoxMeta {
	if bm, ok := meta.ByBoxID()[ref]; ok {
		return bm
	}
	if bm, ok := meta.ByIndexName()[ref]; ok {
		return bm
	}
	var matches []*models.BoxMeta
	for _, bm := range meta.BoxMetas {
		if strings.Contains(bm.Name, ref) {
			matches = append(matches, bm)
		}
	}
	if len(matches) == 1 {
		return matches[0]
	}
	return nil
}

func idsOf(metas []*models.BoxMeta) map[string]bool {
	out := make(map[string]bool, len(metas))
	for _, bm := range metas {
		out[bm.BoxID()] = true
	}
	return out
}

func filterByIDs(metas []*models.BoxMeta, keep map[string]bool) []*models.BoxMeta {
	var out []*models.BoxMeta
	for _, bm := range metas {
		if keep[bm.BoxID()] {
			out = append(out, bm)
		}
	}
	return out
}

// printGroupsView draws the boxes grouped by group.
//
// KNOWN DEVIATION: the Python styles this view with rich (bold group names, a
// dim suffix); this prints plain text. The CONTENT is identical line for line —
// verified against the real yard with the escapes stripped — and nothing
// scripts this view, which is the human browse surface (`-o json` is what
// callers parse). Reproducing rich's colour decisions, which depend on the
// terminal and on env vars, would be a rabbit hole for no gain.
func printGroupsView(cfg *config.Config, metas []*models.BoxMeta, hideGroups []string, showStatus bool) {
	hidden := map[string]bool{}
	for _, g := range hideGroups {
		hidden[g] = true
	}

	byGroup := map[string][]*models.BoxMeta{}
	var ungrouped []*models.BoxMeta
	for _, bm := range metas {
		visible := 0
		for _, g := range bm.Groups {
			if hidden[g] {
				continue
			}
			byGroup[g] = append(byGroup[g], bm)
			visible++
		}
		if len(bm.Groups) == 0 {
			ungrouped = append(ungrouped, bm)
		}
	}

	names := make([]string, 0, len(byGroup))
	for g := range byGroup {
		names = append(names, g)
	}
	sort.Strings(names)

	// This view is a rich Tree in the Python: the group name is bold, the
	// trailing "[other, groups]" is dim, and every label is WRAPPED to the
	// console width with the guides carried down the continuation lines. All
	// three are part of the output — the wrapping in a pipe as much as on a
	// terminal, which is why the port printing one long line per box was a
	// content difference and not only a styling one.
	root := &richstyle.TreeNode{Label: "boxyard"}
	for _, g := range names {
		node := root.Add("[bold]" + richstyle.Escape(g) + "[/bold]")
		addGroupBoxes(node, cfg, byGroup[g], g, showStatus)
	}
	if len(ungrouped) > 0 {
		node := root.Add("[dim](ungrouped)[/dim]")
		addGroupBoxes(node, cfg, ungrouped, "", showStatus)
	}
	printTree(root)
}

// addGroupBoxes hangs one group's boxes off its node, name-sorted, each with
// the groups it belongs to OTHER than this one.
func addGroupBoxes(node *richstyle.TreeNode, cfg *config.Config,
	metas []*models.BoxMeta, group string, showStatus bool) {

	boxes := append([]*models.BoxMeta{}, metas...)
	sort.SliceStable(boxes, func(a, b int) bool { return boxes[a].Name < boxes[b].Name })
	for _, bm := range boxes {
		var others []string
		for _, g := range bm.Groups {
			if group == "" || g != group {
				others = append(others, g)
			}
		}
		sort.Strings(others)
		suffix := ""
		if len(others) > 0 {
			// The Python escapes this bracket by hand (`\\[`) so rich prints
			// it instead of reading it as a style tag.
			suffix = ` [dim]\[` + strings.Join(others, ", ") + "][/dim]"
		}
		node.Add(fmt.Sprintf("%s%s (%s)%s",
			statusMarker(cfg, bm, showStatus), richstyle.Escape(bm.Name), bm.BoxID(), suffix))
	}
}

// printTree renders a rich Tree to stdout exactly as a default rich Console
// would: its width, its terminal detection, its wrapping.
func printTree(root *richstyle.TreeNode) {
	lines, err := richstyle.RenderTree(root, richstyle.ConsoleWidth(),
		richstyle.Enabled(), richstyle.NoColor())
	if err != nil {
		// Unreachable for markup this file builds; a panic here would be a bug
		// in the builder, and swallowing it would print a silently wrong tree.
		panic(err)
	}
	for _, line := range lines {
		fmt.Println(line)
	}
}

// printListTreeView draws the parent/child tree over the FILTERED set.
//
// A box whose parents are all outside that set is treated as a root here —
// otherwise a group filter would make its subtree disappear entirely rather
// than reparent it.
func printListTreeView(cfg *config.Config, metas []*models.BoxMeta, showStatus bool) {
	filtered := &models.BoxyardMeta{BoxMetas: metas}
	inSet := idsOf(metas)

	var roots []*models.BoxMeta
	for _, bm := range metas {
		isRoot := len(bm.Parents) == 0
		if !isRoot {
			isRoot = true
			for _, p := range bm.Parents {
				if inSet[p] {
					isRoot = false
					break
				}
			}
		}
		if isRoot {
			roots = append(roots, bm)
		}
	}
	sort.Slice(roots, func(i, j int) bool { return roots[i].IndexName() < roots[j].IndexName() })

	// A rich Tree in the Python too, so it wraps to the console width just as
	// the groups view does. Its labels are plain rich Text rather than markup
	// — the `[groups: ...]` suffix was being eaten as a style tag until
	// v0.5.9 — so they are escaped here and carry no styling.
	shown := map[string]bool{}
	var add func(node *richstyle.TreeNode, bm *models.BoxMeta)
	add = func(node *richstyle.TreeNode, bm *models.BoxMeta) {
		child := node.Add(richstyle.Escape(listTreeLabel(cfg, bm, showStatus)))
		shown[bm.BoxID()] = true
		children := filtered.ChildrenOf(bm.BoxID())
		sort.Slice(children, func(i, j int) bool { return children[i].IndexName() < children[j].IndexName() })
		for _, c := range children {
			if !shown[c.BoxID()] {
				add(child, c)
			}
		}
	}

	root := &richstyle.TreeNode{Label: "boxyard"}
	for _, r := range roots {
		add(root, r)
	}
	printTree(root)
}

func listTreeLabel(cfg *config.Config, bm *models.BoxMeta, showStatus bool) string {
	groups := ""
	if len(bm.Groups) > 0 {
		groups = " [groups: " + strings.Join(bm.Groups, ", ") + "]"
	}
	return fmt.Sprintf("%s%s (%s)%s", statusMarker(cfg, bm, showStatus), bm.Name, bm.BoxID(), groups)
}

func treeBranch(last bool) string {
	if last {
		return "└── "
	}
	return "├── "
}

func treeIndent(last bool) string {
	if last {
		return "    "
	}
	return "│   "
}

package cli

import (
	"fmt"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
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

	fmt.Println("boxyard")
	names := make([]string, 0, len(byGroup))
	for g := range byGroup {
		names = append(names, g)
	}
	sort.Strings(names)

	type branch struct {
		label string
		boxes []*models.BoxMeta
		group string
	}
	branches := make([]branch, 0, len(names)+1)
	for _, g := range names {
		branches = append(branches, branch{label: g, boxes: byGroup[g], group: g})
	}
	if len(ungrouped) > 0 {
		branches = append(branches, branch{label: "(ungrouped)", boxes: ungrouped})
	}

	for i, br := range branches {
		last := i == len(branches)-1
		fmt.Println(treeBranch(last) + br.label)
		boxes := append([]*models.BoxMeta{}, br.boxes...)
		sort.Slice(boxes, func(a, b int) bool { return boxes[a].Name < boxes[b].Name })
		for j, bm := range boxes {
			var others []string
			for _, g := range bm.Groups {
				if br.group == "" || g != br.group {
					others = append(others, g)
				}
			}
			sort.Strings(others)
			suffix := ""
			if len(others) > 0 {
				suffix = " [" + strings.Join(others, ", ") + "]"
			}
			fmt.Printf("%s%s%s%s (%s)%s\n",
				treeIndent(last), treeBranch(j == len(boxes)-1),
				statusMarker(cfg, bm, showStatus), bm.Name, bm.BoxID(), suffix)
		}
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

	shown := map[string]bool{}
	var draw func(bm *models.BoxMeta, prefix string, last bool)
	draw = func(bm *models.BoxMeta, prefix string, last bool) {
		fmt.Println(prefix + treeBranch(last) + listTreeLabel(cfg, bm, showStatus))
		shown[bm.BoxID()] = true
		children := filtered.ChildrenOf(bm.BoxID())
		sort.Slice(children, func(i, j int) bool { return children[i].IndexName() < children[j].IndexName() })
		var pending []*models.BoxMeta
		for _, c := range children {
			if !shown[c.BoxID()] {
				pending = append(pending, c)
			}
		}
		next := prefix + "│   "
		if last {
			next = prefix + "    "
		}
		for i, c := range pending {
			draw(c, next, i == len(pending)-1)
		}
	}

	fmt.Println("boxyard")
	for i, r := range roots {
		draw(r, "", i == len(roots)-1)
	}
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

package richstyle

import (
	"encoding/json"
	"os/exec"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/pyref"
)

// The three tree views (`list --view groups`, `list --view tree`, `tree`) are
// rich Trees in the Python, and rich does three things to them that a port
// forgets: it draws the guides, it WRAPS every label to the console width with
// the guides carried down, and it CROPS the result to that width — which is
// why a wrapped line sometimes keeps its trailing space and sometimes does
// not. The wrapping is a content difference, visible in a pipe, not a styling
// one; on a real yard it changes how many lines the view has.
//
// This compares the renderer against rich itself over trees built to hit all
// of that, so it needs no yard and touches nothing.
const pyTreeDriver = `
import json, sys, io, os
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("NO_COLOR", None)
from rich.console import Console
from rich.tree import Tree

def build(node, spec):
    for child in spec:
        build(node.add(child["label"]), child.get("children", []))

out = []
for spec, width, enable, no_colour in json.loads(sys.argv[1]):
    tree = Tree(spec["label"])
    build(tree, spec.get("children", []))
    c = Console(file=io.StringIO(), force_terminal=bool(enable), no_color=bool(no_colour),
                color_system="truecolor" if enable else None, width=width)
    c.print(tree)
    out.append(c.file.getvalue().split("\n"))
print(json.dumps(out))
`

type treeSpec struct {
	Label    string     `json:"label"`
	Children []treeSpec `json:"children,omitempty"`
}

func (s treeSpec) node() *TreeNode {
	n := &TreeNode{Label: s.Label}
	for _, c := range s.Children {
		n.Children = append(n.Children, c.node())
	}
	return n
}

func TestRenderTreeMatchesRich(t *testing.T) {
	py := pyref.Bin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}

	long := "scope-out-other-countries-with-similar-public-datasets-as-sri-lanka-for-politick"
	specs := []treeSpec{
		{Label: "boxyard"},
		{Label: "boxyard", Children: []treeSpec{{Label: "one"}, {Label: "two"}}},
		// The shape `list --view groups` builds: a bold group with dim-suffixed
		// boxes under it, the suffix bracket hand-escaped as the Python does.
		{Label: "boxyard", Children: []treeSpec{
			{Label: "[bold]archived[/bold]", Children: []treeSpec{
				{Label: `● care-visa-sponsorship-database (20250324_000000_IjtzW) [dim]\[adu-team, archived, ctx/macbook][/dim]`},
				{Label: `○ short (20260822_tsl6xn)`},
				// A label whose break lands INSIDE the escaped bracket, which
				// is where a markup-string split silently loses the break.
				{Label: `● ` + long + ` (20260407_p8xbj2) [dim]\[a-group, another-group, third-group][/dim]`},
			}},
			{Label: "[dim](ungrouped)[/dim]", Children: []treeSpec{
				{Label: `● ` + long + ` (20260407_p8xbj2)`},
			}},
		}},
		// Nesting deeper than one level, so the guides accumulate.
		{Label: "boxyard", Children: []treeSpec{
			{Label: `root-box (20260822_aaaaa) \[groups: one, two]`, Children: []treeSpec{
				{Label: `child-box-with-a-fairly-long-name (20260822_bbbbb) \[groups: three]`, Children: []treeSpec{
					{Label: "grandchild-" + long + " (20260822_ccccc)"},
				}},
				{Label: "second-child (20260822_ddddd)"},
			}},
			{Label: `\[unknown parent]`, Children: []treeSpec{{Label: "orphan (20260822_eeeee)"}}},
		}},
		// Wide and zero-width characters, where the wrap is measured in cells.
		{Label: "boxyard", Children: []treeSpec{
			{Label: "日本語のボックス名前がとても長い場合はどこで折り返されるか (20260822_fffff)"},
			{Label: `åéîøü-accented-box-name-that-is-long-enough-to-wrap (20260822_ggggg) \[groups: x]`},
			{Label: "emoji 🚀 box name long enough to need wrapping past the eightieth cell (20260822_hhhhh)"},
		}},
	}

	widths := []int{200, 120, 80, 60, 40, 25, 12}
	type flags struct{ enable, noColour bool }
	combos := []flags{{true, false}, {false, false}, {true, true}}

	var cases [][4]any
	var want [][]string
	for _, spec := range specs {
		for _, w := range widths {
			for _, f := range combos {
				cases = append(cases, [4]any{spec, w, f.enable, f.noColour})
				lines, err := RenderTree(spec.node(), w, f.enable, f.noColour)
				if err != nil {
					t.Fatalf("RenderTree(%v, %d): %v", spec.Label, w, err)
				}
				// rich's Console ends with a newline, so its split gives a
				// trailing empty element.
				want = append(want, append(append([]string{}, lines...), ""))
			}
		}
	}

	payload, err := json.Marshal(cases)
	if err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(py, "-c", pyTreeDriver, string(payload)).Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatal(err)
	}
	var got [][]string
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}
	if len(got) != len(want) {
		t.Fatalf("rich rendered %d trees, this package %d", len(got), len(want))
	}

	mismatches := 0
	for i := range got {
		if strings.Join(got[i], "\n") == strings.Join(want[i], "\n") {
			continue
		}
		mismatches++
		if mismatches <= 3 {
			c := cases[i]
			t.Errorf("tree %d (width=%v enable=%v no_colour=%v):\n  rich:\n    %s\n  go:\n    %s",
				i, c[1], c[2], c[3],
				strings.Join(quoteAll(got[i]), "\n    "),
				strings.Join(quoteAll(want[i]), "\n    "))
		}
	}
	if mismatches > 3 {
		t.Errorf("... and %d more mismatching trees", mismatches-3)
	}
	if !strings.Contains(strings.Join(got[0], ""), "boxyard") {
		t.Fatal("rich rendered nothing — the comparison is vacuous")
	}
}

func quoteAll(lines []string) []string {
	out := make([]string, len(lines))
	for i, l := range lines {
		out[i] = strings.TrimSuffix(strings.TrimPrefix(escapeGo(l), `"`), `"`)
	}
	return out
}

func escapeGo(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '\x1b':
			b.WriteString(`\e`)
		case '"':
			b.WriteString(`\"`)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
	return b.String()
}

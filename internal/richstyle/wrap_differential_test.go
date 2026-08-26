package richstyle

import (
	"encoding/json"
	"os/exec"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/pyref"
)

// rich is the reference for the wrapping too. Its divide_line has three
// behaviours that a reasonable-looking reimplementation gets wrong — a word
// carries its trailing whitespace, the fit test measures the word without it
// while the offset advances with it, and an over-long word is FOLDED by cells
// rather than pushed to the next line — so this compares against the real
// thing over inputs built to hit each of them.
const pyWrapDriver = `
import json, sys
from rich._wrap import divide_line
from rich.cells import cell_len, chop_cells
from rich.cells import load_cell_table

cases = json.loads(sys.argv[1])
out = []
for kind, payload in cases:
    if kind == "cell_len":
        out.append(cell_len(payload))
    elif kind == "chop":
        text, width = payload
        out.append(chop_cells(text, width))
    elif kind == "table":
        out.append([list(row) for row in load_cell_table("auto").widths])
    else:
        text, width = payload
        breaks = divide_line(text, width)
        lines = []
        prev = 0
        for b in breaks:
            lines.append(text[prev:b])
            prev = b
        lines.append(text[prev:])
        out.append(lines)
print(json.dumps(out))
`

var wrapTexts = []string{
	"short",
	"",
	"   ",
	"care-visa-sponsorship-database (20250324_000000_IjtzW) [adu-team, archived, ctx/macbook]",
	"scope-out-other-countries-with-similar-public-datasets-as-sri-lanka-for-politick (20260407_p8xbj2)",
	// A single word longer than the width, which must FOLD rather than move.
	strings.Repeat("x", 200),
	"tiny " + strings.Repeat("y", 90) + " tail",
	// Runs of whitespace, which the word regex sweeps up with the word.
	"a    b    c",
	"  leading space then a very long stretch of words that has to wrap somewhere sensible",
	"trailing space at the very end of a line that wraps here     ",
	// Non-ASCII: one-cell accents, two-cell CJK, and a zero-width combining
	// mark — the three widths the table distinguishes.
	"åéîøü-a-box-with-accents (20260822_tsl6xn) [ctx/mac, archived, dormant, and-more]",
	"日本語のボックス名前がとても長い場合はどこで折り返されるか (20260822_tsl6xn)",
	"mixed 日本語 and latin text that is long enough to need wrapping at some point here",
	"écombininǵ markś that are zero width repeated many times over and over again",
	"emoji 🚀 in a box name that is long enough to wrap somewhere past the eightieth cell",
}

var wrapWidths = []int{80, 72, 40, 20, 10, 3, 1}

func TestWrapMatchesRich(t *testing.T) {
	py := pyref.Bin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}

	var cases [][2]any
	for _, text := range wrapTexts {
		cases = append(cases, [2]any{"cell_len", text})
		for _, w := range wrapWidths {
			cases = append(cases, [2]any{"wrap", []any{text, w}})
			cases = append(cases, [2]any{"chop", []any{text, w}})
		}
	}

	payload, err := json.Marshal(cases)
	if err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(py, "-c", pyWrapDriver, string(payload)).Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatal(err)
	}
	var got []json.RawMessage
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}
	if len(got) != len(cases) {
		t.Fatalf("rich answered %d cases, %d were asked", len(got), len(cases))
	}

	for i, c := range cases {
		switch c[0] {
		case "cell_len":
			var want int
			if err := json.Unmarshal(got[i], &want); err != nil {
				t.Fatal(err)
			}
			if g := CellLen(c[1].(string)); g != want {
				t.Errorf("CellLen(%q) = %d, rich = %d", c[1], g, want)
			}
		case "wrap":
			var want []string
			if err := json.Unmarshal(got[i], &want); err != nil {
				t.Fatal(err)
			}
			args := c[1].([]any)
			text, width := args[0].(string), args[1].(int)
			g := Wrap(text, width)
			if strings.Join(g, "\x00") != strings.Join(want, "\x00") {
				t.Errorf("Wrap(%q, %d):\n  rich: %q\n  go:   %q", text, width, want, g)
			}
		case "chop":
			var want []string
			if err := json.Unmarshal(got[i], &want); err != nil {
				t.Fatal(err)
			}
			args := c[1].([]any)
			text, width := args[0].(string), args[1].(int)
			g := chopCells(text, width)
			if strings.Join(g, "\x00") != strings.Join(want, "\x00") {
				t.Errorf("chopCells(%q, %d):\n  rich: %q\n  go:   %q", text, width, want, g)
			}
		}
	}
}

// The cell table is generated FROM rich. This fails when a dependency bump
// changes it, which is the only way that drift would ever be noticed.
func TestCellTableMatchesRich(t *testing.T) {
	py := pyref.Bin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}
	payload, _ := json.Marshal([][2]any{{"table", nil}})
	out, err := exec.Command(py, "-c", pyWrapDriver, string(payload)).Output()
	if err != nil {
		t.Fatal(err)
	}
	var wrapped [][][3]int32
	if err := json.Unmarshal(out, &wrapped); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}
	want := wrapped[0]
	if len(want) != len(cellTable) {
		t.Fatalf("rich's table has %d entries, celltable.go has %d — regenerate it",
			len(want), len(cellTable))
	}
	for i := range want {
		if want[i] != cellTable[i] {
			t.Fatalf("entry %d: rich has %v, celltable.go has %v — regenerate it",
				i, want[i], cellTable[i])
		}
	}
}

package cli

import (
	"encoding/json"
	"os/exec"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/pyref"
)

// A differential of the `include -I` / `exclude -I` picker lines against the
// installed Python.
//
// These lines are what the user actually reads before confirming a batch
// exclusion — the wrong glyph or a size rounded the other way is a person
// deleting the wrong box off a machine. Both are pure formatting, so they can
// be compared exactly, with no yard involved.

const pyPickDriver = `
import json, sys

cases = json.loads(sys.argv[1])
out = []
for kind, payload in cases:
    if kind == "size":
        b = payload
        size_gb = b / (1024 ** 3)
        if size_gb >= 0.1:
            out.append(f"{size_gb:.1f} GB")
        else:
            out.append(f"{b / (1024 ** 2):.0f} MB")
    else:
        name, box_id, groups = payload
        group_tag = f" [{', '.join(groups)}]" if groups else ""
        # Transcribed from cli_include / cli_exclude in _cli/main.py. The
        # size prefix there is built from a real size; the exact value does
        # not matter to the format, only that the column and its two
        # trailing spaces are reproduced.
        size_str = "[1.5 GB]  "
        if kind == "include":
            out.append(f"\u25cb {name} ({box_id}){group_tag}")
        elif kind == "include-confirm":
            out.append(f"  {name} ({box_id})")
        elif kind == "exclude":
            out.append(f"\u25cf {name} ({box_id}){group_tag}")
        elif kind == "exclude-confirm":
            out.append(f"  {name} ({box_id})")
        elif kind == "exclude-sized":
            out.append(f"{size_str}\u25cf {name} ({box_id}){group_tag}")
        else:
            out.append(f"  {size_str}{name} ({box_id})")
print(json.dumps(out))
`

func TestPickerLinesMatchPython(t *testing.T) {
	py := pyref.Bin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}

	const gb = int64(1024 * 1024 * 1024)
	const mb = int64(1024 * 1024)
	sizes := []int64{
		0, 1, mb, 99 * mb, 100 * mb,
		gb/10 - 1, gb / 10, gb/10 + 1, // the MB/GB boundary
		gb, gb + gb/20, 3*gb + gb/2, 1536 * mb,
		512 * gb, 1024 * gb,
		// Half-way values: Python's format and Go's %.Nf both round half to
		// even, and a naive round-half-up would disagree here.
		gb*105/100 - 1, 157286400,
	}

	type boxCase struct {
		name, ts, subid string
		groups          []string
	}
	boxes := []boxCase{
		{"boxyard", "20251122_143022", "a7kx9", nil},
		{"my-thing", "20251122_143022", "b1234", []string{"work"}},
		{"\u00e5\u00e9\u00ee\u00f8\u00fc", "20251122_143022", "c5678", []string{"ctx/mac", "archived"}},
		{"weird[name]", "20251122_143022", "d9012", []string{"a", "b", "c"}},
		{"has space", "20251122_143022", "e3456", []string{}},
	}

	// Every line either picker can print. The confirm variants matter as much
	// as the fzf ones: they are the last thing shown before a batch exclusion
	// deletes boxes off the machine.
	kinds := []string{
		"include", "include-confirm",
		"exclude", "exclude-confirm",
		"exclude-sized", "exclude-sized-confirm",
	}
	const sizePrefix = "[1.5 GB]  "

	var cases [][2]any
	var want []string
	for _, b := range sizes {
		cases = append(cases, [2]any{"size", b})
		want = append(want, formatSize(b))
	}
	for _, kind := range kinds {
		for _, b := range boxes {
			cases = append(cases, [2]any{kind, []any{b.name, b.ts + "_" + b.subid, b.groups}})
			// The Go side goes through the SAME functions the commands call,
			// so a changed glyph or a lost space fails here.
			bm := &models.BoxMeta{Name: b.name, CreationTimestampUTC: b.ts, BoxSubid: b.subid, Groups: b.groups}
			switch kind {
			case "include":
				want = append(want, includePickLine(bm))
			case "include-confirm":
				want = append(want, includeConfirmLine(bm))
			case "exclude":
				want = append(want, excludePickLine(bm, ""))
			case "exclude-confirm":
				want = append(want, excludeConfirmLine(bm, ""))
			case "exclude-sized":
				want = append(want, excludePickLine(bm, sizePrefix))
			case "exclude-sized-confirm":
				want = append(want, excludeConfirmLine(bm, sizePrefix))
			}
		}
	}

	payload, err := json.Marshal(cases)
	if err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(py, "-c", pyPickDriver, string(payload)).Output()
	if err != nil {
		t.Fatalf("running the Python driver: %v", err)
	}
	var got []string
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("decoding the Python output %q: %v", out, err)
	}
	if len(got) != len(want) {
		t.Fatalf("Python produced %d lines, Go %d", len(got), len(want))
	}
	for i := range got {
		if got[i] != want[i] {
			t.Errorf("case %d (%v): python=%q go=%q", i, cases[i], got[i], want[i])
		}
	}
	if t.Failed() {
		t.Logf("compared %d cases", len(got))
	}
	joined := strings.Join(got, "")
	for _, marker := range []string{"○", "●", "[1.5 GB]  ●"} {
		if !strings.Contains(joined, marker) {
			t.Fatalf("the driver produced no %q lines — the comparison is vacuous", marker)
		}
	}
}

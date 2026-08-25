package parity

import (
	"encoding/json"
	"os/exec"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/boxref"
)

// A differential of the name-matching rules against the installed Python.
//
// These decide which box ~15 commands act on, and the three modes are easy to
// get subtly wrong — `contains` and `subsequence` agree on most inputs and
// disagree on exactly the ones that matter. The Python is the reference.
const pyMatchDriver = `
import json, sys
from boxyard._cli.main import _is_subsequence_match

cases = json.loads(sys.argv[1])
out = []
for term, name, mode, match_case in cases:
    t, n = (term, name) if match_case else (term.lower(), name.lower())
    if mode == "exact":
        hit = n == t
    elif mode == "contains":
        hit = t in n
    else:
        hit = _is_subsequence_match(t, n)
    out.append(hit)
print(json.dumps(out))
`

func TestNameMatchingMatchesPython(t *testing.T) {
	py := pythonBin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}

	names := []string{
		"boxyard", "boxyard-go", "Sesh", "myrig", "my-virtual-assistant-ideation",
		"", ".dotted", "UPPER", "with space", "åéîøü", "a", "aa",
	}
	terms := []string{
		"boxyard", "BOXYARD", "go", "sesh", "myg", "", "a", "aa", "aaa",
		"åé", "éå", " ", "-", "z",
	}
	modes := []boxref.MatchMode{boxref.MatchExact, boxref.MatchContains, boxref.MatchSubsequence}

	type pyCase [4]any
	var cases []pyCase
	type goCase struct {
		term, name string
		mode       boxref.MatchMode
		matchCase  bool
	}
	var goCases []goCase

	for _, name := range names {
		for _, term := range terms {
			for _, mode := range modes {
				for _, matchCase := range []bool{true, false} {
					cases = append(cases, pyCase{term, name, string(mode), matchCase})
					goCases = append(goCases, goCase{term, name, mode, matchCase})
				}
			}
		}
	}

	payload, err := json.Marshal(cases)
	if err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(py, "-c", pyMatchDriver, string(payload)).Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatalf("python driver failed: %v", err)
	}
	var want []bool
	if err := json.Unmarshal(out, &want); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}
	if len(want) != len(goCases) {
		t.Fatalf("driver returned %d results for %d cases", len(want), len(goCases))
	}

	mismatches := 0
	for i, c := range goCases {
		got := goMatch(c.term, c.name, c.mode, c.matchCase)
		if got != want[i] {
			mismatches++
			if mismatches <= 10 {
				t.Errorf("term=%q name=%q mode=%s case=%v: Go=%v Python=%v",
					c.term, c.name, c.mode, c.matchCase, got, want[i])
			}
		}
	}
	if mismatches > 0 {
		t.Fatalf("%d/%d comparisons differ", mismatches, len(goCases))
	}
	t.Logf("%d comparisons, 0 mismatches", len(goCases))
}

// goMatch mirrors boxref.filterByName's per-box decision. Kept here rather than
// exported so the production path stays a filter over BoxMetas.
func goMatch(term, name string, mode boxref.MatchMode, matchCase bool) bool {
	if !matchCase {
		term, name = lower(term), lower(name)
	}
	switch mode {
	case boxref.MatchExact:
		return name == term
	case boxref.MatchContains:
		return contains(name, term)
	default:
		return boxref.IsSubsequenceMatch(term, name)
	}
}

func lower(s string) string     { return strings.ToLower(s) }
func contains(s, t string) bool { return strings.Contains(s, t) }

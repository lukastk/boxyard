package models

import (
	"bytes"
	"encoding/json"
	"os/exec"
	"testing"

	"github.com/lukastk/boxyard/internal/pyref"
)

// The merge is a pure function of three boxmetas, which makes it the ideal
// shape for a differential: the whole input space can be generated and both
// implementations asked the same questions.
//
// It matters more than most. The two implementations will run side by side
// across the fleet during the rollout, and a merge they disagree about does
// not fail loudly — it converges to two different boxmetas that each machine
// keeps pushing at the other.
const pyMergeDriver = `
import json, sys
from boxyard._models import BoxMeta, MetaMergeConflict, merge_box_metas

# Read the cases from STDIN, not argv: the full input space is well past the
# system's argument-length limit, and truncating the space to fit it would be
# the wrong trade in a differential.
CASES = json.load(sys.stdin)

def meta(spec):
    return BoxMeta(
        creation_timestamp_utc="20260822_000000",
        box_subid="aaaaa",
        name="a-box",
        storage_location="remote",
        creator_hostname=spec.get("creator_hostname") or "host",
        # An "or" rather than a get-default: Go marshals a nil slice as JSON
        # null, so the key is PRESENT and the default never applies. (No
        # backticks in this string -- it is a Go raw literal.)
        groups=spec.get("groups") or [],
        parents=spec.get("parents") or [],
        write_owner=spec.get("write_owner"),
        unknown_keys=spec.get("unknown_keys") or {},
    )

out = []
for base, local, remote in CASES:
    try:
        m = merge_box_metas(meta(base), meta(local), meta(remote))
    except MetaMergeConflict as e:
        out.append({"conflict": e.fields})
        continue
    out.append({
        "groups": m.groups,
        "parents": m.parents,
        "write_owner": m.write_owner,
        "creator_hostname": m.creator_hostname,
        "unknown_keys": m.unknown_keys,
    })
print(json.dumps(out))
`

type mergeSpec struct {
	Groups          []string       `json:"groups"`
	Parents         []string       `json:"parents"`
	WriteOwner      *string        `json:"write_owner"`
	CreatorHostname string         `json:"creator_hostname"`
	UnknownKeys     map[string]any `json:"unknown_keys"`
}

type mergeResult struct {
	Conflict        []string       `json:"conflict"`
	Groups          []string       `json:"groups"`
	Parents         []string       `json:"parents"`
	WriteOwner      *string        `json:"write_owner"`
	CreatorHostname string         `json:"creator_hostname"`
	UnknownKeys     map[string]any `json:"unknown_keys"`
}

func (s mergeSpec) box() *BoxMeta {
	owner := ""
	if s.WriteOwner != nil {
		owner = *s.WriteOwner
	}
	host := s.CreatorHostname
	if host == "" {
		host = "host"
	}
	groups, parents := s.Groups, s.Parents
	if groups == nil {
		groups = []string{}
	}
	if parents == nil {
		parents = []string{}
	}
	unknown := s.UnknownKeys
	if unknown == nil {
		unknown = map[string]any{}
	}
	return &BoxMeta{
		CreationTimestampUTC: "20260822_000000",
		BoxSubid:             "aaaaa",
		Name:                 "a-box",
		StorageLocation:      "remote",
		CreatorHostname:      host,
		Groups:               groups,
		Parents:              parents,
		WriteOwner:           owner,
		UnknownKeys:          unknown,
	}
}

func ptr(s string) *string { return &s }

// subsets enumerates every subset of a universe, in a stable order.
func subsets(universe []string) [][]string {
	out := [][]string{}
	for mask := 0; mask < 1<<len(universe); mask++ {
		var s []string
		for i, u := range universe {
			if mask&(1<<i) != 0 {
				s = append(s, u)
			}
		}
		if s == nil {
			s = []string{}
		}
		out = append(out, s)
	}
	return out
}

func TestMergeMatchesPython(t *testing.T) {
	py := pyref.Bin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}

	var cases [][3]mergeSpec

	// Every (base, local, remote) over a three-group universe: 512 triples,
	// which covers every add/remove/keep combination on both sides at once.
	universe := []string{"a", "b", "c"}
	all := subsets(universe)
	for _, b := range all {
		for _, l := range all {
			for _, r := range all {
				cases = append(cases, [3]mergeSpec{{Groups: b}, {Groups: l}, {Groups: r}})
			}
		}
	}

	// Every combination of write_owner across unowned / two machines.
	owners := []*string{nil, ptr("macbook"), ptr("mymain")}
	for _, b := range owners {
		for _, l := range owners {
			for _, r := range owners {
				cases = append(cases, [3]mergeSpec{
					{WriteOwner: b}, {WriteOwner: l}, {WriteOwner: r},
				})
			}
		}
	}

	// Parents, and keys written by a newer boxyard.
	parentUniverse := subsets([]string{"20260101_aaaaa", "20260202_bbbbb"})
	for _, b := range parentUniverse {
		for _, l := range parentUniverse {
			for _, r := range parentUniverse {
				cases = append(cases, [3]mergeSpec{{Parents: b}, {Parents: l}, {Parents: r}})
			}
		}
	}
	unknowns := []map[string]any{
		{},
		{"k": float64(1)},
		{"k": float64(2)},
		{"other": "x"},
		{"k": float64(1), "other": "x"},
	}
	for _, b := range unknowns {
		for _, l := range unknowns {
			for _, r := range unknowns {
				cases = append(cases, [3]mergeSpec{
					{UnknownKeys: b}, {UnknownKeys: l}, {UnknownKeys: r},
				})
			}
		}
	}

	payload, err := json.Marshal(cases)
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(py, "-c", pyMergeDriver)
	cmd.Stdin = bytes.NewReader(payload)
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatal(err)
	}
	var want []mergeResult
	if err := json.Unmarshal(out, &want); err != nil {
		t.Fatalf("decoding %q: %v", out[:min(len(out), 400)], err)
	}
	if len(want) != len(cases) {
		t.Fatalf("python answered %d cases, %d were asked", len(want), len(cases))
	}

	mismatches, conflicts := 0, 0
	for i, c := range cases {
		got, err := MergeBoxMetas(c[0].box(), c[1].box(), c[2].box())
		w := want[i]

		if len(w.Conflict) > 0 {
			conflicts++
			var mc *MetaMergeConflict
			if err == nil {
				t.Errorf("case %d: python refused (%v), go merged", i, w.Conflict)
				mismatches++
				continue
			}
			if !asConflict(err, &mc) {
				t.Errorf("case %d: python refused (%v), go failed differently: %v", i, w.Conflict, err)
				mismatches++
				continue
			}
			if !equalStr(mc.Fields, w.Conflict) {
				t.Errorf("case %d: conflicting fields python=%v go=%v", i, w.Conflict, mc.Fields)
				mismatches++
			}
			continue
		}

		if err != nil {
			t.Errorf("case %d (%+v): python merged, go refused: %v", i, c, err)
			mismatches++
			continue
		}
		// Lists, not sets: two machines that agree on the content but not its
		// order trade the same boxmeta forever.
		if !equalStr(got.Groups, w.Groups) || !equalStr(got.Parents, w.Parents) {
			t.Errorf("case %d (%+v):\n  python groups=%v parents=%v\n  go     groups=%v parents=%v",
				i, c, w.Groups, w.Parents, got.Groups, got.Parents)
			mismatches++
		}
		wantOwner := ""
		if w.WriteOwner != nil {
			wantOwner = *w.WriteOwner
		}
		if got.WriteOwner != wantOwner || got.CreatorHostname != w.CreatorHostname {
			t.Errorf("case %d: python owner=%q host=%q, go owner=%q host=%q",
				i, wantOwner, w.CreatorHostname, got.WriteOwner, got.CreatorHostname)
			mismatches++
		}
		if !equalAnyMap(got.UnknownKeys, w.UnknownKeys) {
			t.Errorf("case %d: python unknown=%v go=%v", i, w.UnknownKeys, got.UnknownKeys)
			mismatches++
		}
		if mismatches > 5 {
			t.Fatalf("stopping after %d mismatches of %d cases", mismatches, len(cases))
		}
	}
	if conflicts == 0 {
		t.Error("no case produced a conflict — the refusal path is not covered")
	}
	t.Logf("compared %d merges (%d of them refusals)", len(cases), conflicts)
}

func asConflict(err error, target **MetaMergeConflict) bool {
	if c, ok := err.(*MetaMergeConflict); ok {
		*target = c
		return true
	}
	return false
}

func equalStr(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func equalAnyMap(a, b map[string]any) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if bv, ok := b[k]; !ok || bv != v {
			return false
		}
	}
	return true
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

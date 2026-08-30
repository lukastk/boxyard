package syncpolicy

import (
	"bytes"
	"encoding/json"
	"os/exec"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/pyref"
)

// Policy resolution is a pure function of (config, box groups), which makes it
// the right shape for a differential: the input space can be enumerated and
// both implementations asked the same questions.
//
// It matters more than most. The two implementations will run side by side
// across the fleet during the Go rollout, and a cadence they disagree about
// does not fail loudly — one machine simply syncs a box on a schedule the other
// does not, and the divergence shows up as "why is this box stale".

const pyIntervalDriver = `
import json, sys
from boxyard.config import parse_interval

CASES = json.load(sys.stdin)
out = []
for text in CASES:
    try:
        out.append({"ok": True, "seconds": parse_interval(text, "where")})
    except Exception as e:
        out.append({"ok": False, "error": type(e).__name__})
print(json.dumps(out))
`

func TestParseIntervalMatchesPython(t *testing.T) {
	py := pyref.Bin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}
	if !pyref.HasSymbol("boxyard.config", "parse_interval") {
		t.Skip("installed boxyard has no config.parse_interval yet — " +
			"this differential goes live when the Python release carrying the " +
			"sync-cadence work reaches this machine")
	}

	cases := []string{
		// Accepted forms.
		"1s", "30s", "59s", "1m", "15m", "90m", "1h", "6h", "12h", "24h",
		"1d", "7d", "30d", "1w", "2w", "100h",
		// Rejected: no unit.
		"6", "0", "", " ", "\t",
		// Rejected: unknown unit.
		"6x", "6y", "6M", "sixh", "h", "s",
		// Rejected: not a whole number.
		"6.5h", "-1h", "+1h", "1_000h", "1e3h", " 6 h",
		// Rejected: zero.
		"0h", "0s", "0d",
		// Rejected: compound.
		"1h30m", "1d2h",
		// Case handling.
		"6H", "7D", "15M", "1W",
		// Leading/trailing space is trimmed by both.
		" 6h", "6h ", "  6h  ",
	}

	payload, err := json.Marshal(cases)
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(py, "-c", pyIntervalDriver)
	cmd.Stdin = bytes.NewReader(payload)
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatal(err)
	}
	var want []struct {
		OK      bool   `json:"ok"`
		Seconds int    `json:"seconds"`
		Error   string `json:"error"`
	}
	if err := json.Unmarshal(out, &want); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}
	if len(want) != len(cases) {
		t.Fatalf("python answered %d cases, %d were asked", len(want), len(cases))
	}

	accepted, rejected := 0, 0
	for i, text := range cases {
		seconds, err := config.ParseInterval(text, "where")
		if want[i].OK {
			accepted++
			if err != nil {
				t.Errorf("%q: python accepted (%ds), go refused: %v", text, want[i].Seconds, err)
				continue
			}
			if seconds != want[i].Seconds {
				t.Errorf("%q: python=%ds go=%ds", text, want[i].Seconds, seconds)
			}
			continue
		}
		rejected++
		if err == nil {
			t.Errorf("%q: python refused, go accepted it as %ds", text, seconds)
		}
	}
	// Guards against a vacuous run: a driver that errored on everything, or a
	// case list that only exercises one side.
	if accepted == 0 || rejected == 0 {
		t.Fatalf("one-sided comparison: %d accepted, %d rejected", accepted, rejected)
	}
	t.Logf("compared %d intervals (%d accepted, %d refused)", len(cases), accepted, rejected)
}

const pyResolveDriver = `
import json, sys
from pathlib import Path

from boxyard._models import BoxMeta
from boxyard._sync_policy import PolicyConflict, resolve_policy
from boxyard.config import Config

CASES = json.load(sys.stdin)

BASE = {
    "config_path": Path("/tmp/x/config.toml"),
    "default_storage_location": "remote",
    "boxyard_data_path": Path("/tmp/boxyard-differential-does-not-exist"),
    "box_timestamp_format": "date_only",
    "user_boxes_path": Path("/tmp/boxes"),
    "user_box_groups_path": Path("/tmp/box-groups"),
    "storage_locations": {"remote": {"storage_type": "local", "store_path": "/tmp/store"}},
    "box_groups": {},
    "virtual_box_groups": {},
    "default_box_groups": [],
    "box_subid_character_set": "abcdefghijklmnopqrstuvwxyz0123456789",
    "box_subid_length": 5,
    "max_concurrent_rclone_ops": 3,
}

out = []
for policies, groups in CASES:
    config = Config(**{**BASE, "sync_policies": policies})
    box = BoxMeta(
        creation_timestamp_utc="20260822", box_subid="aaaaa", name="a-box",
        storage_location="remote", creator_hostname="host",
        groups=groups, parents=[],
    )
    try:
        r = resolve_policy(config, box)
    except PolicyConflict as e:
        out.append({"conflict": e.dimension})
        continue
    except Exception as e:
        out.append({"error": type(e).__name__})
        continue
    out.append({
        "data": r.data_interval_seconds,
        "meta": r.meta_interval_seconds,
    })
print(json.dumps(out))
`

type resolveCase struct {
	Policies map[string]map[string]any `json:"-"`
	Groups   []string                  `json:"-"`
}

func TestResolvePolicyMatchesPython(t *testing.T) {
	py := pyref.Bin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}
	if !pyref.HasSymbol("boxyard._sync_policy", "resolve_policy") {
		t.Skip("installed boxyard has no _sync_policy module yet — " +
			"this differential goes live when the Python release carrying the " +
			"sync-cadence work reaches this machine")
	}

	// The fleet's real shape plus the shapes that decide the design: a policy
	// stating only some dimensions, two policies agreeing, two disagreeing, and
	// `default` also naming a group.
	policySets := []map[string]map[string]any{
		{},
		{"default": {"data_interval": "6h", "meta_interval": "15m"}},
		{
			"default": {"data_interval": "6h", "meta_interval": "15m"},
			"cold":    {"data_interval": "7d", "meta_interval": "30m", "groups": []string{"archived", "dormant"}},
		},
		{
			"default": {"data_interval": "6h"},
			"cold":    {"data_interval": "7d", "groups": []string{"archived"}},
			"hot":     {"data_interval": "1h", "groups": []string{"live"}},
		},
		{
			"default": {"data_interval": "6h"},
			"cold":    {"data_interval": "7d", "groups": []string{"archived"}},
			"slow":    {"data_interval": "7d", "groups": []string{"dormant"}},
		},
		{
			"default": {"data_interval": "6h", "groups": []string{"archived"}},
			"cold":    {"data_interval": "7d", "groups": []string{"archived"}},
		},
		{
			"default": {"meta_interval": "15m"},
			"plain":   {"data_interval": "1h", "groups": []string{"live"}},
		},
	}
	groupSets := [][]string{
		{}, {"proj"}, {"archived"}, {"dormant"}, {"live"},
		{"archived", "dormant"}, {"archived", "live"}, {"archived", "proj"},
		{"live", "dormant"}, {"archived", "dormant", "live"},
	}

	type pair struct {
		P map[string]map[string]any
		G []string
	}
	var cases []pair
	for _, p := range policySets {
		for _, g := range groupSets {
			cases = append(cases, pair{p, g})
		}
	}

	wire := make([][2]any, 0, len(cases))
	for _, c := range cases {
		wire = append(wire, [2]any{c.P, c.G})
	}
	payload, err := json.Marshal(wire)
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(py, "-c", pyResolveDriver)
	cmd.Stdin = bytes.NewReader(payload)
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatal(err)
	}
	var want []struct {
		Data     *int    `json:"data"`
		Meta     *int    `json:"meta"`
		Conflict *string `json:"conflict"`
		Error    *string `json:"error"`
	}
	if err := json.Unmarshal(out, &want); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}
	if len(want) != len(cases) {
		t.Fatalf("python answered %d cases, %d were asked", len(want), len(cases))
	}

	conflicts, resolutions := 0, 0
	for i, c := range cases {
		cfg := buildConfig(t, c.P)
		bm := &models.BoxMeta{
			CreationTimestampUTC: "20260822", BoxSubid: "aaaaa", Name: "a-box",
			StorageLocation: "remote", CreatorHostname: "host",
			Groups: c.G, Parents: []string{},
		}
		got, err := ResolvePolicy(cfg, bm)

		if want[i].Conflict != nil {
			conflicts++
			var conflict *PolicyConflict
			if err == nil {
				t.Errorf("case %d (%v): python refused on %q, go resolved", i, c.G, *want[i].Conflict)
				continue
			}
			if !asConflict(err, &conflict) {
				t.Errorf("case %d: python refused on %q, go failed differently: %v", i, *want[i].Conflict, err)
				continue
			}
			if conflict.Dimension != *want[i].Conflict {
				t.Errorf("case %d: conflicting dimension python=%q go=%q", i, *want[i].Conflict, conflict.Dimension)
			}
			continue
		}
		if err != nil {
			t.Errorf("case %d (%v, %v): python resolved, go refused: %v", i, c.P, c.G, err)
			continue
		}
		resolutions++

		gotData := (*int)(nil)
		if got.HasDataInterval {
			v := got.DataIntervalSeconds
			gotData = &v
		}
		gotMeta := (*int)(nil)
		if got.HasMetaInterval {
			v := got.MetaIntervalSeconds
			gotMeta = &v
		}
		if !intPtrEqual(gotData, want[i].Data) || !intPtrEqual(gotMeta, want[i].Meta) {
			t.Errorf("case %d (%v):\n  python data=%v meta=%v\n  go     data=%v meta=%v",
				i, c.G, deref(want[i].Data), deref(want[i].Meta), deref(gotData), deref(gotMeta))
		}
	}
	if conflicts == 0 {
		t.Error("no case produced a conflict — the refusal path is not covered")
	}
	if resolutions == 0 {
		t.Error("no case resolved — the comparison is vacuous")
	}
	t.Logf("compared %d resolutions (%d of them refusals)", len(cases), conflicts)
}

func buildConfig(t *testing.T, policies map[string]map[string]any) *config.Config {
	t.Helper()
	cfg := &config.Config{
		DefaultStorageLocation: "remote",
		BoxyardDataPath:        t.TempDir(),
		UserBoxesPath:          "/tmp/boxes",
		UserBoxGroupsPath:      "/tmp/box-groups",
		StorageLocations:       map[string]*config.StorageConfig{},
		SyncPolicies:           map[string]*config.SyncPolicyConfig{},
	}
	for name, spec := range policies {
		p := &config.SyncPolicyConfig{}
		if v, ok := spec["data_interval"].(string); ok {
			p.DataInterval = v
		}
		if v, ok := spec["meta_interval"].(string); ok {
			p.MetaInterval = v
		}
		if v, ok := spec["groups"].([]string); ok {
			p.Groups = v
		}
		cfg.SyncPolicies[name] = p
	}
	return cfg
}

func intPtrEqual(a, b *int) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

func deref(p *int) any {
	if p == nil {
		return nil
	}
	return *p
}

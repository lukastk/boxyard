package models

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
)

func mk(ts, subid, name string, parents ...string) *BoxMeta {
	if parents == nil {
		parents = []string{}
	}
	return &BoxMeta{
		CreationTimestampUTC: ts, BoxSubid: subid, Name: name,
		StorageLocation: "remote", CreatorHostname: "h",
		Groups: []string{}, Parents: parents,
	}
}

// Two records copied VERBATIM out of the live 586-box boxyard_meta.json written
// by Python v0.5.5 — an owned box and an unowned one. Field order and the
// absence of spaces are part of the contract: this file is read by mysystem's
// TypeScript BoxyardService and by myrig's picker as well as by both
// implementations.
//
// The previous fixture was a PRE-v0.5.0 sample, so it carried neither
// write_owner nor unknown_keys — and the test passed happily while every Go
// command failed on the real file with `unknown field "unknown_keys"`. A frozen
// fixture only ever proves parity with the day it was taken; both shapes are
// pinned here now, and the UNOWNED one matters most, because `write_owner:
// null` covers 318 of the 586 boxes on this fleet.
const goldenMeta = `{"box_metas":[{"creation_timestamp_utc":"20260601","box_subid":"rh9q4r","name":"scuttlebug-ui__stream-10-codex-auth","storage_location":"hetzner-box","creator_hostname":"Lukas’s MacBook Pro","groups":["ctx/macbook","worktrees","archived"],"parents":[],"write_owner":"macbook","unknown_keys":{}},{"creation_timestamp_utc":"20250622_000000","box_subid":"aTrMF","name":"install-magpy-test","storage_location":"hetzner-box","creator_hostname":"Lukas’s MacBook Pro","groups":["null","archived","ctx/macbook"],"parents":[],"write_owner":null,"unknown_keys":{}}]}`

func TestMetaRoundTripsPythonBytes(t *testing.T) {
	var m BoxyardMeta
	if err := unmarshalMeta([]byte(goldenMeta), &m); err != nil {
		t.Fatalf("could not parse a Python-written registry: %v", err)
	}
	if len(m.BoxMetas) != 2 {
		t.Fatalf("parsed %d boxes, want 2", len(m.BoxMetas))
	}
	if m.BoxMetas[0].WriteOwner != "macbook" {
		t.Errorf("owned box: write_owner = %q", m.BoxMetas[0].WriteOwner)
	}
	if m.BoxMetas[1].WriteOwner != "" {
		t.Errorf("unowned box: write_owner = %q, want empty", m.BoxMetas[1].WriteOwner)
	}
	out, err := m.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != goldenMeta {
		t.Errorf("byte round trip failed\n got: %s\nwant: %s", out, goldenMeta)
	}
}

// Go marshals a nil slice as null; pydantic emits []. The TypeScript reader
// would break on null.
func TestNilSlicesMarshalAsEmptyArrays(t *testing.T) {
	m := &BoxyardMeta{BoxMetas: []*BoxMeta{{
		CreationTimestampUTC: "20260601", BoxSubid: "aaaaaa", Name: "x",
		StorageLocation: "remote", CreatorHostname: "h",
	}}}
	out, err := m.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	// `"write_owner":null` is legitimate — pydantic emits it for an unowned
	// box — so only the slice fields are checked for null.
	for _, bad := range []string{`"groups":null`, `"parents":null`, `"unknown_keys":null`} {
		if strings.Contains(string(out), bad) {
			t.Errorf("marshalled %s: %s", bad, out)
		}
	}
	if !strings.Contains(string(out), `"groups":[]`) || !strings.Contains(string(out), `"parents":[]`) {
		t.Errorf("expected empty arrays, got: %s", out)
	}
}

func TestEmptyRegistryMarshalsAsEmptyArray(t *testing.T) {
	m := &BoxyardMeta{}
	out, err := m.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != `{"box_metas":[]}` {
		t.Errorf("empty registry = %s", out)
	}
}

func TestCreateScansLocalStoreAndSkipsBroken(t *testing.T) {
	cfg := testConfig(t)
	store := filepath.Join(cfg.LocalStorePath(), "remote")

	good := mk("20260601", "aaaaaa", "good")
	if err := good.Save(cfg); err != nil {
		t.Fatal(err)
	}
	// A registration directory with no boxmeta.toml.
	if err := os.MkdirAll(filepath.Join(store, "20260601_bbbbbb__broken"), 0o755); err != nil {
		t.Fatal(err)
	}
	// A stray file, which must be ignored rather than treated as a box.
	if err := os.WriteFile(filepath.Join(store, "stray.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	// A hidden directory: Python's glob("*") DOES include dotfiles, so this is
	// scanned (and, having no boxmeta.toml, reported as broken).
	if err := os.MkdirAll(filepath.Join(store, ".hidden"), 0o755); err != nil {
		t.Fatal(err)
	}

	meta, broken, err := CreateBoxyardMeta(cfg)
	if err != nil {
		t.Fatalf("CreateBoxyardMeta: %v", err)
	}
	if len(meta.BoxMetas) != 1 || meta.BoxMetas[0].Name != "good" {
		t.Errorf("expected only the good box, got %d", len(meta.BoxMetas))
	}
	if len(broken) != 2 {
		var names []string
		for _, b := range broken {
			names = append(names, b.Registration)
		}
		t.Errorf("expected 2 broken registrations (the empty dir and the dotfile dir), got %d: %v", len(broken), names)
	}
}

// A storage location with nothing checked out locally is a legitimate state.
func TestCreateToleratesMissingStorageDir(t *testing.T) {
	cfg := testConfig(t)
	meta, broken, err := CreateBoxyardMeta(cfg)
	if err != nil {
		t.Fatalf("a local store that does not exist yet should not be an error: %v", err)
	}
	if len(meta.BoxMetas) != 0 || len(broken) != 0 {
		t.Errorf("expected an empty registry, got %d boxes / %d broken", len(meta.BoxMetas), len(broken))
	}
}

func TestRefreshWritesAtomicallyAndGetReads(t *testing.T) {
	cfg := testConfig(t)
	if err := mk("20260601", "aaaaaa", "one").Save(cfg); err != nil {
		t.Fatal(err)
	}
	if _, err := RefreshBoxyardMeta(cfg, false); err != nil {
		t.Fatalf("Refresh: %v", err)
	}
	if _, err := os.Stat(cfg.BoxyardMetaPath()); err != nil {
		t.Fatalf("registry not written: %v", err)
	}
	// No .tmp left behind.
	entries, _ := os.ReadDir(cfg.BoxyardDataPath)
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".tmp") {
			t.Errorf("atomic write left %s behind", e.Name())
		}
	}
	got, err := GetBoxyardMeta(cfg, false)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if len(got.BoxMetas) != 1 || got.BoxMetas[0].Name != "one" {
		t.Errorf("read back %+v", got.BoxMetas)
	}
}

func TestGetBuildsRegistryWhenAbsent(t *testing.T) {
	cfg := testConfig(t)
	if err := mk("20260601", "aaaaaa", "one").Save(cfg); err != nil {
		t.Fatal(err)
	}
	got, err := GetBoxyardMeta(cfg, false)
	if err != nil {
		t.Fatalf("Get with no cache: %v", err)
	}
	if len(got.BoxMetas) != 1 {
		t.Errorf("expected the registry to be built on demand, got %d boxes", len(got.BoxMetas))
	}
}

// --- DAG ---

func diamond() *BoxyardMeta {
	//     a
	//    / \
	//   b   c
	//    \ /
	//     d
	a := mk("20260101", "aaaaaa", "a")
	b := mk("20260102", "bbbbbb", "b", a.BoxID())
	c := mk("20260103", "cccccc", "c", a.BoxID())
	d := mk("20260104", "dddddd", "d", b.BoxID(), c.BoxID())
	return &BoxyardMeta{BoxMetas: []*BoxMeta{a, b, c, d}}
}

func names(metas []*BoxMeta) string {
	var out []string
	for _, m := range metas {
		out = append(out, m.Name)
	}
	return strings.Join(out, ",")
}

func TestDAG(t *testing.T) {
	m := diamond()
	byName := map[string]*BoxMeta{}
	for _, bm := range m.BoxMetas {
		byName[bm.Name] = bm
	}
	a, b, d := byName["a"], byName["b"], byName["d"]

	if got := names(m.ChildrenOf(a.BoxID())); got != "b,c" {
		t.Errorf("ChildrenOf(a) = %q", got)
	}
	// d must appear exactly once despite two paths to it.
	if got := names(m.DescendantsOf(a.BoxID())); got != "b,c,d" {
		t.Errorf("DescendantsOf(a) = %q, want b,c,d (d deduplicated)", got)
	}
	if got := names(m.AncestorsOf(d.BoxID())); got != "b,c,a" {
		t.Errorf("AncestorsOf(d) = %q", got)
	}
	if got := names(m.Roots()); got != "a" {
		t.Errorf("Roots = %q", got)
	}
	if got := names(m.Leaves()); got != "d" {
		t.Errorf("Leaves = %q", got)
	}

	if !m.WouldCreateCycle(a.BoxID(), d.BoxID()) {
		t.Error("making d a parent of a should be a cycle")
	}
	if !m.WouldCreateCycle(a.BoxID(), a.BoxID()) {
		t.Error("a box cannot be its own parent")
	}
	if m.WouldCreateCycle(b.BoxID(), a.BoxID()) {
		t.Error("b already has a as a parent; that is not a new cycle")
	}
}

func TestLookupIsLoud(t *testing.T) {
	m := diamond()
	if _, err := m.Lookup("nope"); err == nil {
		t.Fatal("unknown index name returned no error")
	} else if !strings.Contains(err.Error(), "nope") {
		t.Errorf("error should name the box, got: %v", err)
	}
	if _, err := m.Lookup(m.BoxMetas[0].IndexName()); err != nil {
		t.Errorf("known box errored: %v", err)
	}
}

func TestGroupConfigsAddsImplicitGroups(t *testing.T) {
	cfg := testConfig(t)
	cfg.BoxGroups = map[string]*config.BoxGroupConfig{
		"declared": {BoxTitleMode: config.TitleName},
	}
	bm := mk("20260101", "aaaaaa", "a")
	bm.Groups = []string{"declared", "implicit"}

	groups, _ := GroupConfigs(cfg, []*BoxMeta{bm})
	if len(groups) != 2 {
		t.Fatalf("expected 2 groups, got %v", groups)
	}
	if groups["declared"].BoxTitleMode != config.TitleName {
		t.Error("declared group config was overwritten")
	}
	if groups["implicit"].BoxTitleMode != config.TitleIndexName {
		t.Errorf("implicit group should default to index_name, got %q", groups["implicit"].BoxTitleMode)
	}
}

func TestByIndexes(t *testing.T) {
	m := diamond()
	if len(m.ByIndexName()) != 4 || len(m.ByBoxID()) != 4 {
		t.Error("index maps are incomplete")
	}
	bySL := m.ByStorageLocation()
	if len(bySL["remote"]) != 4 {
		t.Errorf("ByStorageLocation = %v", bySL)
	}
}

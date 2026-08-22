package models

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
)

// testConfig builds a Config directly, avoiding a config.toml round trip.
func testConfig(t *testing.T) *config.Config {
	t.Helper()
	root := t.TempDir()
	return &config.Config{
		ConfigPath:             filepath.Join(root, "config", "config.toml"),
		DefaultStorageLocation: "remote",
		BoxyardDataPath:        filepath.Join(root, "data"),
		BoxTimestampFormat:     config.TimestampDateOnly,
		UserBoxesPath:          filepath.Join(root, "boxes"),
		UserBoxGroupsPath:      filepath.Join(root, "groups"),
		StorageLocations: map[string]*config.StorageConfig{
			"remote": {StorageType: config.StorageRclone, StorePath: "boxyard"},
		},
		BoxGroups:              map[string]*config.BoxGroupConfig{},
		VirtualBoxGroups:       map[string]*config.VirtualBoxGroupConfig{},
		DefaultBoxGroups:       []string{},
		BoxSubidCharacterSet:   "abcdefghijklmnopqrstuvwxyz0123456789",
		BoxSubidLength:         6,
		MaxConcurrentRcloneOps: 2,
	}
}

func sampleMeta() *BoxMeta {
	return &BoxMeta{
		CreationTimestampUTC: "20260601",
		BoxSubid:             "rh9q4r",
		Name:                 "scuttlebug-ui",
		StorageLocation:      "remote",
		CreatorHostname:      "mymain",
		Groups:               []string{"ctx/macbook", "worktrees"},
		Parents:              []string{},
	}
}

// These goldens came from the live Python `tomli_w.dumps` and were confirmed
// by a differential run over 12 cases — all 12 byte-identical, and all
// round-tripping back through tomllib to the original values.
func TestRenderMatchesPythonToml(t *testing.T) {
	cases := []struct {
		name string
		meta BoxMeta
		want string
	}{
		{
			name: "typical, with unicode hostname",
			meta: BoxMeta{
				StorageLocation: "hetzner-box",
				CreatorHostname: "Lukas’s MacBook Pro",
				Groups:          []string{"ctx/macbook", "worktrees"},
				Parents:         []string{},
			},
			want: "storage_location = \"hetzner-box\"\ncreator_hostname = \"Lukas’s MacBook Pro\"\ngroups = [\n    \"ctx/macbook\",\n    \"worktrees\",\n]\nparents = []\n",
		},
		{
			name: "empty lists",
			meta: BoxMeta{StorageLocation: "fake", CreatorHostname: "h", Groups: []string{}, Parents: []string{}},
			want: "storage_location = \"fake\"\ncreator_hostname = \"h\"\ngroups = []\nparents = []\n",
		},
		{
			name: "single entries are still one per line",
			meta: BoxMeta{StorageLocation: "fake", CreatorHostname: "h", Groups: []string{"a"}, Parents: []string{"20260101_abc"}},
			want: "storage_location = \"fake\"\ncreator_hostname = \"h\"\ngroups = [\n    \"a\",\n]\nparents = [\n    \"20260101_abc\",\n]\n",
		},
		{
			name: "quotes are escaped",
			meta: BoxMeta{StorageLocation: "s", CreatorHostname: `he said "hi"`, Groups: []string{"a"}, Parents: []string{}},
			want: "storage_location = \"s\"\ncreator_hostname = \"he said \\\"hi\\\"\"\ngroups = [\n    \"a\",\n]\nparents = []\n",
		},
		{
			name: "backslashes are escaped",
			meta: BoxMeta{StorageLocation: "s", CreatorHostname: `back\slash`, Groups: []string{"a"}, Parents: []string{}},
			want: "storage_location = \"s\"\ncreator_hostname = \"back\\\\slash\"\ngroups = [\n    \"a\",\n]\nparents = []\n",
		},
		{
			name: "nil lists render as empty, not null",
			meta: BoxMeta{StorageLocation: "s", CreatorHostname: "h"},
			want: "storage_location = \"s\"\ncreator_hostname = \"h\"\ngroups = []\nparents = []\n",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := c.meta.Render(); got != c.want {
				t.Errorf("byte mismatch with Python toml.dumps\n got: %q\nwant: %q", got, c.want)
			}
		})
	}
}

func TestRejectsControlCharactersInHostname(t *testing.T) {
	for _, ch := range []string{"\x00", "\x07", "\x1b", "\x7f", "\n", "\t", "\r"} {
		m := sampleMeta()
		m.CreatorHostname = "host" + ch + "name"
		if err := m.Validate(); err == nil {
			t.Errorf("accepted a control character %q in a hostname", ch)
		}
	}
	for _, h := range []string{"mymain", "Lukas’s MacBook Pro", "host-1.local", "日本語ホスト"} {
		m := sampleMeta()
		m.CreatorHostname = h
		if err := m.Validate(); err != nil {
			t.Errorf("rejected ordinary hostname %q: %v", h, err)
		}
	}
}

func TestBoxIDAndIndexName(t *testing.T) {
	m := sampleMeta()
	if got := m.BoxID(); got != "20260601_rh9q4r" {
		t.Errorf("BoxID = %q", got)
	}
	if got := m.IndexName(); got != "20260601_rh9q4r__scuttlebug-ui" {
		t.Errorf("IndexName = %q", got)
	}
}

func TestParseIndexName(t *testing.T) {
	cases := []struct{ in, id, name string }{
		{"20260601_rh9q4r__scuttlebug-ui", "20260601_rh9q4r", "scuttlebug-ui"},
		// Legacy date-and-time ids, and mixed-case subids, both exist in the
		// real yard.
		{"20250622_000000_aTrMF__install-magpy-test", "20250622_000000_aTrMF", "install-magpy-test"},
		// A name containing "__" must not be split again.
		{"20260601_rh9q4r__scuttlebug-ui__stream-10", "20260601_rh9q4r", "scuttlebug-ui__stream-10"},
		// Two real boxes have SPACES in their names.
		{"20240805_000000_E4Dzy__Notion integration test", "20240805_000000_E4Dzy", "Notion integration test"},
	}
	for _, c := range cases {
		id, name, err := ParseIndexName(c.in)
		if err != nil {
			t.Fatalf("%q: %v", c.in, err)
		}
		if id != c.id || name != c.name {
			t.Errorf("%q -> (%q, %q), want (%q, %q)", c.in, id, name, c.id, c.name)
		}
	}
	if _, _, err := ParseIndexName("no-separator"); err == nil {
		t.Error("index name without __ was accepted")
	}
}

func TestSplitBoxID(t *testing.T) {
	cases := []struct {
		in, ts, subid string
		wantErr       bool
	}{
		{in: "20260601_rh9q4r", ts: "20260601", subid: "rh9q4r"},
		{in: "20250622_000000_aTrMF", ts: "20250622_000000", subid: "aTrMF"},
		{in: "nounderscore", wantErr: true},
		{in: "a_b_c_d", wantErr: true},
	}
	for _, c := range cases {
		ts, subid, err := splitBoxID(c.in)
		if c.wantErr {
			if err == nil {
				t.Errorf("%q was accepted", c.in)
			}
			continue
		}
		if err != nil || ts != c.ts || subid != c.subid {
			t.Errorf("%q -> (%q,%q,%v)", c.in, ts, subid, err)
		}
	}
}

func TestCreationTimestampBothFormats(t *testing.T) {
	m := sampleMeta()
	ts, err := m.CreationTimestamp()
	if err != nil || ts.Format("2006-01-02") != "2026-06-01" {
		t.Errorf("date-only: %v %v", ts, err)
	}
	m.CreationTimestampUTC = "20250622_000000"
	if ts, err = m.CreationTimestamp(); err != nil || ts.Format("2006-01-02") != "2025-06-22" {
		t.Errorf("date-and-time: %v %v", ts, err)
	}
	m.CreationTimestampUTC = "not-a-date"
	if _, err = m.CreationTimestamp(); err == nil {
		t.Error("invalid timestamp accepted")
	}
}

func TestPaths(t *testing.T) {
	cfg := testConfig(t)
	m := sampleMeta()
	idx := m.IndexName()

	remote, err := m.RemotePath(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if want := "boxyard/boxes/" + idx; remote != want {
		t.Errorf("RemotePath = %q, want %q", remote, want)
	}

	if got, want := m.LocalPath(cfg), filepath.Join(cfg.LocalStorePath(), "remote", idx); got != want {
		t.Errorf("LocalPath = %q, want %q", got, want)
	}

	// DATA is the odd one out: it lives in the user's boxes dir, not the store.
	data, _ := m.LocalPartPath(cfg, enums.PartData)
	if want := filepath.Join(cfg.UserBoxesPath, idx); data != want {
		t.Errorf("local DATA = %q, want %q", data, want)
	}
	meta, _ := m.LocalPartPath(cfg, enums.PartMeta)
	if want := filepath.Join(m.LocalPath(cfg), "boxmeta.toml"); meta != want {
		t.Errorf("local META = %q, want %q", meta, want)
	}
	conf, _ := m.LocalPartPath(cfg, enums.PartConf)
	if want := filepath.Join(m.LocalPath(cfg), "conf"); conf != want {
		t.Errorf("local CONF = %q, want %q", conf, want)
	}

	rd, _ := m.RemotePartPath(cfg, enums.PartData)
	if want := "boxyard/boxes/" + idx + "/data"; rd != want {
		t.Errorf("remote DATA = %q, want %q", rd, want)
	}

	rsr, _ := m.RemoteSyncRecordPath(cfg, enums.PartData)
	if want := "boxyard/sync_records/" + idx + "/data.rec"; rsr != want {
		t.Errorf("remote sync record = %q, want %q", rsr, want)
	}
	lsr := m.LocalSyncRecordPath(cfg, enums.PartMeta)
	if want := filepath.Join(cfg.BoxyardDataPath, "sync_records", idx, "meta.rec"); lsr != want {
		t.Errorf("local sync record = %q, want %q", lsr, want)
	}

	if _, err := m.RemotePartPath(cfg, enums.BoxPart("bogus")); err == nil {
		t.Error("invalid box part accepted")
	}
}

func TestValidate(t *testing.T) {
	cases := []struct {
		name    string
		mutate  func(*BoxMeta)
		wantErr string
	}{
		{"duplicate groups", func(m *BoxMeta) { m.Groups = []string{"a", "a"} }, "unique"},
		{"invalid group name", func(m *BoxMeta) { m.Groups = []string{"bad name"} }, "group name"},
		{"duplicate parents", func(m *BoxMeta) { m.Parents = []string{"x", "x"} }, "unique"},
		{"self parent", func(m *BoxMeta) { m.Parents = []string{"20260601_rh9q4r"} }, "own parent"},
		{"bad timestamp", func(m *BoxMeta) { m.CreationTimestampUTC = "nope" }, "timestamp"},
		{"missing storage location", func(m *BoxMeta) { m.StorageLocation = "" }, "storage_location"},
		{"missing hostname", func(m *BoxMeta) { m.CreatorHostname = "" }, "creator_hostname"},
		{"nil groups", func(m *BoxMeta) { m.Groups = nil }, "groups"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			m := sampleMeta()
			c.mutate(m)
			err := m.Validate()
			if err == nil {
				t.Fatalf("accepted an invalid BoxMeta")
			}
			if !strings.Contains(err.Error(), c.wantErr) {
				t.Errorf("error should mention %q, got: %v", c.wantErr, err)
			}
		})
	}
	if err := sampleMeta().Validate(); err != nil {
		t.Errorf("valid BoxMeta rejected: %v", err)
	}
}

func TestSaveLoadRoundTrip(t *testing.T) {
	cfg := testConfig(t)
	m := sampleMeta()
	m.Parents = []string{"20250101_aaaaaa"}
	if err := m.Save(cfg); err != nil {
		t.Fatalf("Save: %v", err)
	}

	loaded, err := LoadBoxMeta(cfg, "remote", m.IndexName())
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if loaded.IndexName() != m.IndexName() {
		t.Errorf("index name changed: %q -> %q", m.IndexName(), loaded.IndexName())
	}
	if loaded.CreatorHostname != m.CreatorHostname || loaded.StorageLocation != m.StorageLocation {
		t.Errorf("fields changed: %+v", loaded)
	}
	if strings.Join(loaded.Groups, ",") != strings.Join(m.Groups, ",") {
		t.Errorf("groups changed: %v", loaded.Groups)
	}
	if strings.Join(loaded.Parents, ",") != strings.Join(m.Parents, ",") {
		t.Errorf("parents changed: %v", loaded.Parents)
	}

	// No .tmp file should survive the atomic write.
	entries, _ := os.ReadDir(m.LocalPath(cfg))
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".tmp") {
			t.Errorf("atomic write left %s behind", e.Name())
		}
	}
}

// A boxmeta.toml written before `parents` existed has no such key. That is a
// legitimate expected state, not a parse error.
func TestLoadBackwardsCompatibleWithoutParents(t *testing.T) {
	cfg := testConfig(t)
	m := sampleMeta()
	idx := m.IndexName()
	dir := filepath.Join(cfg.LocalStorePath(), "remote", idx)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := "storage_location = \"remote\"\ncreator_hostname = \"old-host\"\ngroups = [ \"a\",]\n"
	if err := os.WriteFile(filepath.Join(dir, "boxmeta.toml"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	loaded, err := LoadBoxMeta(cfg, "remote", idx)
	if err != nil {
		t.Fatalf("legacy boxmeta.toml rejected: %v", err)
	}
	if len(loaded.Parents) != 0 {
		t.Errorf("parents should default to empty, got %v", loaded.Parents)
	}
}

func TestLoadRejectsUnknownKeys(t *testing.T) {
	cfg := testConfig(t)
	idx := sampleMeta().IndexName()
	dir := filepath.Join(cfg.LocalStorePath(), "remote", idx)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := "storage_location = \"remote\"\ncreator_hostname = \"h\"\ngroups = []\nparents = []\nsurprise = 1\n"
	if err := os.WriteFile(filepath.Join(dir, "boxmeta.toml"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadBoxMeta(cfg, "remote", idx); err == nil {
		t.Fatal("unknown key in boxmeta.toml was silently accepted")
	}
}

func TestLoadMissingFileIsLoud(t *testing.T) {
	cfg := testConfig(t)
	if _, err := LoadBoxMeta(cfg, "remote", "20260601_aaaaaa__nope"); err == nil {
		t.Fatal("missing boxmeta.toml loaded successfully")
	}
}

func TestCheckIncluded(t *testing.T) {
	cfg := testConfig(t)
	m := sampleMeta()
	if m.CheckIncluded(cfg) {
		t.Error("reported included before the data dir exists")
	}
	data, _ := m.LocalPartPath(cfg, enums.PartData)
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	if !m.CheckIncluded(cfg) {
		t.Error("reported not included after the data dir was created")
	}
}

func TestSortByCreation(t *testing.T) {
	metas := []*BoxMeta{
		{CreationTimestampUTC: "20260601", BoxSubid: "c", Name: "c"},
		{CreationTimestampUTC: "20250622_000000", BoxSubid: "a", Name: "a"},
		{CreationTimestampUTC: "20260101", BoxSubid: "b", Name: "b"},
	}
	SortByCreation(metas)
	got := metas[0].Name + metas[1].Name + metas[2].Name
	if got != "abc" {
		t.Errorf("sorted order = %q, want %q", got, "abc")
	}
}

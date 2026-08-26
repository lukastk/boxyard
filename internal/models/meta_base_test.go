package models

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
)

// The rule these tests protect: a STALE base is worse than no base. A merge
// diffs against it, so a base that never corresponded to a real shared state
// produces a confidently wrong answer, where a missing one only makes the
// merge decline.

func baseYard(t *testing.T) (*config.Config, *BoxMeta, string, string) {
	t.Helper()
	root := t.TempDir()
	cfg := &config.Config{
		BoxyardDataPath: filepath.Join(root, "data"),
		UserBoxesPath:   filepath.Join(root, "boxes"),
	}
	bm := &BoxMeta{
		CreationTimestampUTC: "20260822_000000",
		BoxSubid:             "aaaaa",
		Name:                 "a-box",
		StorageLocation:      "remote",
		CreatorHostname:      "test",
		Groups:               []string{"work"},
		Parents:              []string{},
	}
	metaPath, err := bm.LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(metaPath), 0o755); err != nil {
		t.Fatal(err)
	}
	return cfg, bm, metaPath, bm.LocalMetaBasePath(cfg)
}

func writeMeta(t *testing.T, path, groups string) {
	t.Helper()
	body := "creator_hostname = \"test\"\nparents = []\ngroups = " + groups + "\n"
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestRecordMetaBaseWritesAndReplaces(t *testing.T) {
	cfg, bm, metaPath, basePath := baseYard(t)

	writeMeta(t, metaPath, `["work"]`)
	if err := RecordMetaBase(cfg, bm); err != nil {
		t.Fatal(err)
	}
	base, err := ReadMetaBase(cfg, bm)
	if err != nil || base == nil {
		t.Fatalf("base = %v, err = %v", base, err)
	}
	if len(base.Groups) != 1 || base.Groups[0] != "work" {
		t.Errorf("groups = %v", base.Groups)
	}
	// The identity fields are NOT in the file; they come from the box the base
	// belongs to, exactly as LoadBoxMeta derives them from a path.
	if base.IndexName() != bm.IndexName() || base.StorageLocation != bm.StorageLocation {
		t.Errorf("identity not carried: %s / %s", base.IndexName(), base.StorageLocation)
	}

	writeMeta(t, metaPath, `["work", "archived"]`)
	if err := RecordMetaBase(cfg, bm); err != nil {
		t.Fatal(err)
	}
	base, _ = ReadMetaBase(cfg, bm)
	if len(base.Groups) != 2 {
		t.Errorf("the base was not replaced: %v", base.Groups)
	}
	_ = basePath
}

func TestRecordMetaBaseDropsItWhenThereIsNoBoxmeta(t *testing.T) {
	cfg, bm, metaPath, basePath := baseYard(t)
	writeMeta(t, metaPath, `["work"]`)
	if err := RecordMetaBase(cfg, bm); err != nil {
		t.Fatal(err)
	}

	// The box was excluded, or deleted, or never had a boxmeta here. A base
	// left behind would describe a state that no longer exists.
	if err := os.Remove(metaPath); err != nil {
		t.Fatal(err)
	}
	if err := RecordMetaBase(cfg, bm); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(basePath); !os.IsNotExist(err) {
		t.Error("the base survived the boxmeta going away")
	}
}

func TestRecordMetaBaseLeavesNoTempFileBehind(t *testing.T) {
	cfg, bm, metaPath, basePath := baseYard(t)
	writeMeta(t, metaPath, `["work"]`)
	for i := 0; i < 3; i++ {
		if err := RecordMetaBase(cfg, bm); err != nil {
			t.Fatal(err)
		}
	}
	entries, err := os.ReadDir(filepath.Dir(basePath))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Errorf("expected only the base, got %d entries", len(entries))
	}
}

func TestReadMetaBaseWithNoBase(t *testing.T) {
	cfg, bm, _, _ := baseYard(t)
	base, err := ReadMetaBase(cfg, bm)
	if err != nil {
		t.Fatal(err)
	}
	if base != nil {
		t.Error("a box that has never synced its META must read as no base")
	}
}

func TestReadMetaBaseRemovesACorruptBase(t *testing.T) {
	cfg, bm, _, basePath := baseYard(t)
	if err := os.MkdirAll(filepath.Dir(basePath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(basePath, []byte("not = valid toml [[[\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	base, err := ReadMetaBase(cfg, bm)
	if err != nil || base != nil {
		t.Fatalf("base = %v, err = %v", base, err)
	}
	// Removed, not left: merging against a half-read file would be worse than
	// not merging, and leaving it makes every later read pay the same cost.
	if _, err := os.Stat(basePath); !os.IsNotExist(err) {
		t.Error("the corrupt base was left in place")
	}
}

func TestReadMetaBaseCarriesUnknownKeys(t *testing.T) {
	cfg, bm, metaPath, _ := baseYard(t)
	body := "creator_hostname = \"test\"\nparents = []\ngroups = []\nwritten_by_a_newer_boxyard = 1\n"
	if err := os.WriteFile(metaPath, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := RecordMetaBase(cfg, bm); err != nil {
		t.Fatal(err)
	}

	base, err := ReadMetaBase(cfg, bm)
	if err != nil || base == nil {
		t.Fatalf("base = %v, err = %v", base, err)
	}
	// A key from a newer boxyard has to round-trip, or a future merge would
	// compute that the other side ADDED it and this side never had it.
	if _, ok := base.UnknownKeys["written_by_a_newer_boxyard"]; !ok {
		t.Errorf("unknown keys lost: %v", base.UnknownKeys)
	}
}

func TestMetaBaseSitsBesideTheSyncRecord(t *testing.T) {
	cfg, bm, _, basePath := baseYard(t)
	want := filepath.Join(cfg.BoxyardDataPath, boxconst.SyncRecordsRelPath, bm.IndexName(), "meta.base.toml")
	if basePath != want {
		t.Errorf("base path = %s, want %s", basePath, want)
	}
	// Beside the LOCAL record, so it costs no network. That is the whole
	// reason this is cheaper than giving ownership its own synced file.
	if filepath.Dir(basePath) != filepath.Dir(bm.LocalSyncRecordPath(cfg, enums.PartMeta)) {
		t.Error("the base is not beside the local sync record")
	}
}

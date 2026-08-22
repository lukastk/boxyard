package remoteindex

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
)

type fakeStore struct {
	dirs      map[string]bool // "remote:path" -> exists
	listing   []Entry
	listCalls int
	listErr   error
}

func (f *fakeStore) PathExists(_ context.Context, remote, p string) (bool, bool, error) {
	return f.dirs[remote+":"+p], true, nil
}
func (f *fakeStore) ListJSON(_ context.Context, remote, p string) ([]Entry, error) {
	f.listCalls++
	return f.listing, f.listErr
}

func testCfg(t *testing.T) *config.Config {
	t.Helper()
	root := t.TempDir()
	return &config.Config{
		ConfigPath:             filepath.Join(root, "config.toml"),
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
		BoxSubidCharacterSet:   "abc",
		BoxSubidLength:         6,
		MaxConcurrentRcloneOps: 2,
	}
}

func TestLoadMissingCacheIsEmpty(t *testing.T) {
	c := testCfg(t)
	got, err := Load(c, "remote")
	if err != nil || len(got) != 0 {
		t.Fatalf("cold start should be empty: %v %v", got, err)
	}
}

// A corrupt cache is discarded rather than fatal: every entry is re-verified
// against the remote anyway, so a rescan rebuilds it.
func TestLoadCorruptCacheSelfHeals(t *testing.T) {
	c := testCfg(t)
	p := CachePath(c, "remote")
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := Load(c, "remote")
	if err != nil || len(got) != 0 {
		t.Fatalf("corrupt cache should read as empty: %v %v", got, err)
	}
}

// A genuine I/O fault is NOT swallowed — otherwise every command silently does
// a full remote rescan.
func TestLoadUnreadableCacheIsLoud(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root can read anything")
	}
	c := testCfg(t)
	p := CachePath(c, "remote")
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("{}"), 0o000); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(p, 0o644)
	if _, err := Load(c, "remote"); err == nil {
		t.Fatal("an unreadable cache was silently treated as empty")
	}
}

func TestUpdateSaveRemoveRoundTrip(t *testing.T) {
	c := testCfg(t)
	if err := Update(c, "remote", "20260601_aaaaaa", "20260601_aaaaaa__box"); err != nil {
		t.Fatal(err)
	}
	got, _ := Load(c, "remote")
	if got["20260601_aaaaaa"] != "20260601_aaaaaa__box" {
		t.Fatalf("cache = %v", got)
	}
	if err := Remove(c, "remote", "20260601_aaaaaa"); err != nil {
		t.Fatal(err)
	}
	if got, _ = Load(c, "remote"); len(got) != 0 {
		t.Fatalf("entry survived removal: %v", got)
	}
	// Removing something absent is a no-op, not an error.
	if err := Remove(c, "remote", "nope"); err != nil {
		t.Fatalf("removing an absent entry errored: %v", err)
	}
}

func TestFindUsesCacheWhenTheRemoteStillHasIt(t *testing.T) {
	c := testCfg(t)
	if err := Update(c, "remote", "20260601_aaaaaa", "20260601_aaaaaa__box"); err != nil {
		t.Fatal(err)
	}
	s := &fakeStore{dirs: map[string]bool{
		"remote:boxyard/boxes/20260601_aaaaaa__box": true,
	}}
	got, err := Find(context.Background(), s, c, "remote", "20260601_aaaaaa")
	if err != nil || got != "20260601_aaaaaa__box" {
		t.Fatalf("got %q %v", got, err)
	}
	if s.listCalls != 0 {
		t.Error("a cache hit should not have listed the remote")
	}
}

// The box was renamed from another machine: the cached name no longer exists,
// so the cache must be corrected rather than trusted.
func TestFindRepairsStaleCacheEntry(t *testing.T) {
	c := testCfg(t)
	if err := Update(c, "remote", "20260601_aaaaaa", "20260601_aaaaaa__old-name"); err != nil {
		t.Fatal(err)
	}
	s := &fakeStore{
		dirs:    map[string]bool{}, // the cached path is gone
		listing: []Entry{{Name: "20260601_aaaaaa__new-name", IsDir: true}},
	}
	got, err := Find(context.Background(), s, c, "remote", "20260601_aaaaaa")
	if err != nil {
		t.Fatal(err)
	}
	if got != "20260601_aaaaaa__new-name" {
		t.Fatalf("got %q, want the renamed directory", got)
	}
	cache, _ := Load(c, "remote")
	if cache["20260601_aaaaaa"] != "20260601_aaaaaa__new-name" {
		t.Errorf("cache was not corrected: %v", cache)
	}
}

func TestFindMissingBoxReturnsEmptyAndClearsCache(t *testing.T) {
	c := testCfg(t)
	if err := Update(c, "remote", "20260601_aaaaaa", "20260601_aaaaaa__gone"); err != nil {
		t.Fatal(err)
	}
	s := &fakeStore{dirs: map[string]bool{}, listing: []Entry{}}
	got, err := Find(context.Background(), s, c, "remote", "20260601_aaaaaa")
	if err != nil || got != "" {
		t.Fatalf("got %q %v", got, err)
	}
	if cache, _ := Load(c, "remote"); len(cache) != 0 {
		t.Errorf("stale entry survived: %v", cache)
	}
}

// The prefix must be "{box_id}__" — a box id that is a prefix of another must
// not match.
func TestFindDoesNotMatchOnAPartialBoxID(t *testing.T) {
	c := testCfg(t)
	s := &fakeStore{
		dirs: map[string]bool{},
		listing: []Entry{
			{Name: "20260601_aaaaaabb__other", IsDir: true},
			{Name: "20260601_aaaaaa__wanted", IsDir: true},
		},
	}
	got, err := Find(context.Background(), s, c, "remote", "20260601_aaaaaa")
	if err != nil {
		t.Fatal(err)
	}
	if got != "20260601_aaaaaa__wanted" {
		t.Errorf("got %q; a longer box id must not match", got)
	}
}

func TestFindIgnoresFiles(t *testing.T) {
	c := testCfg(t)
	s := &fakeStore{
		dirs:    map[string]bool{},
		listing: []Entry{{Name: "20260601_aaaaaa__box", IsDir: false}},
	}
	got, _ := Find(context.Background(), s, c, "remote", "20260601_aaaaaa")
	if got != "" {
		t.Errorf("a file was treated as a box: %q", got)
	}
}

func TestRebuildIndexesEveryValidDirectory(t *testing.T) {
	c := testCfg(t)
	s := &fakeStore{listing: []Entry{
		{Name: "20260601_aaaaaa__one", IsDir: true},
		{Name: "20250622_000000_bBBBBb__legacy", IsDir: true},
		{Name: "not-an-index-name", IsDir: true},
		{Name: "20260601_cccccc__afile", IsDir: false},
	}}
	cache, err := Rebuild(context.Background(), s, c, "remote")
	if err != nil {
		t.Fatal(err)
	}
	if len(cache) != 2 {
		t.Fatalf("expected 2 entries, got %v", cache)
	}
	if cache["20260601_aaaaaa"] != "20260601_aaaaaa__one" {
		t.Errorf("cache = %v", cache)
	}
	// Legacy date-and-time ids must still index.
	if cache["20250622_000000_bBBBBb"] != "20250622_000000_bBBBBb__legacy" {
		t.Errorf("legacy id not indexed: %v", cache)
	}
	// And it must be persisted.
	onDisk, _ := Load(c, "remote")
	if len(onDisk) != 2 {
		t.Errorf("rebuild did not persist: %v", onDisk)
	}
}

func TestUnknownStorageLocationIsLoud(t *testing.T) {
	c := testCfg(t)
	if _, err := Find(context.Background(), &fakeStore{}, c, "nope", "b"); err == nil {
		t.Fatal("unknown storage location was accepted")
	}
}

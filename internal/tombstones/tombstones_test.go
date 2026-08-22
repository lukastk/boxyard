package tombstones

import (
	"context"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
)

type fakeStore struct {
	files   map[string]string // "remote:path" -> content
	entries map[string][]Entry
	deleted []string
	listNil bool
}

func newFake() *fakeStore {
	return &fakeStore{files: map[string]string{}, entries: map[string][]Entry{}}
}

func key(remote, p string) string { return remote + ":" + p }

func (f *fakeStore) Write(_ context.Context, remote, p, content string) error {
	f.files[key(remote, p)] = content
	return nil
}
func (f *fakeStore) Cat(_ context.Context, remote, p string) (bool, string, error) {
	c, ok := f.files[key(remote, p)]
	return ok, c, nil
}
func (f *fakeStore) PathExists(_ context.Context, remote, p string) (bool, bool, error) {
	_, ok := f.files[key(remote, p)]
	return ok, false, nil
}
func (f *fakeStore) ListJSON(_ context.Context, remote, p string) ([]Entry, error) {
	if f.listNil {
		return nil, nil
	}
	return f.entries[key(remote, p)], nil
}
func (f *fakeStore) Delete(_ context.Context, remote, p string) error {
	delete(f.files, key(remote, p))
	f.deleted = append(f.deleted, key(remote, p))
	return nil
}

func cfg(t *testing.T) *config.Config {
	t.Helper()
	return &config.Config{
		ConfigPath:             "/tmp/x/config.toml",
		DefaultStorageLocation: "remote",
		BoxyardDataPath:        "/tmp/x/data",
		BoxTimestampFormat:     config.TimestampDateOnly,
		UserBoxesPath:          "/tmp/x/boxes",
		UserBoxGroupsPath:      "/tmp/x/groups",
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

func TestRelPath(t *testing.T) {
	if got := RelPath("20260601_rh9q4r"); got != "tombstones/20260601_rh9q4r.json" {
		t.Errorf("got %q", got)
	}
}

func TestCreateWritesToTheStorePath(t *testing.T) {
	s, c := newFake(), cfg(t)
	tomb, err := Create(context.Background(), s, c, "remote", "20260601_rh9q4r", "my-box")
	if err != nil {
		t.Fatal(err)
	}
	if tomb.LastKnownName != "my-box" || tomb.DeletedByHostname == "" {
		t.Errorf("incomplete tombstone: %+v", tomb)
	}
	want := key("remote", "boxyard/tombstones/20260601_rh9q4r.json")
	body, ok := s.files[want]
	if !ok {
		t.Fatalf("tombstone not written to %s; wrote %v", want, s.files)
	}
	if !strings.HasPrefix(body, `{"box_id":"20260601_rh9q4r","deleted_at_utc":"`) {
		t.Errorf("unexpected field order or content: %s", body)
	}
}

func TestIsTombstoned(t *testing.T) {
	s, c := newFake(), cfg(t)
	ok, err := IsTombstoned(context.Background(), s, c, "remote", "nope")
	if err != nil || ok {
		t.Fatalf("absent box reported tombstoned (%v, %v)", ok, err)
	}
	if _, err := Create(context.Background(), s, c, "remote", "b1", "n"); err != nil {
		t.Fatal(err)
	}
	if ok, _ = IsTombstoned(context.Background(), s, c, "remote", "b1"); !ok {
		t.Error("created tombstone not detected")
	}
}

// A missing tombstone is the normal case for almost every box, so it is nil
// rather than an error.
func TestGetMissingIsNotAnError(t *testing.T) {
	s, c := newFake(), cfg(t)
	got, err := Get(context.Background(), s, c, "remote", "nope")
	if err != nil || got != nil {
		t.Fatalf("got %v, %v", got, err)
	}
}

// A tombstone that exists but does not parse means corrupt remote state, and
// must be loud.
func TestGetCorruptIsAnError(t *testing.T) {
	s, c := newFake(), cfg(t)
	s.files[key("remote", "boxyard/tombstones/bad.json")] = `{"box_id":"bad","surprise":1}`
	if _, err := Get(context.Background(), s, c, "remote", "bad"); err == nil {
		t.Fatal("a corrupt tombstone was accepted")
	}
}

func TestGetRoundTripsAPythonWrittenTombstone(t *testing.T) {
	s, c := newFake(), cfg(t)
	const py = `{"box_id":"20260601_rh9q4r","deleted_at_utc":"2026-08-22T21:30:00.123456Z","deleted_by_hostname":"Lukas’s MacBook Pro","last_known_name":"my-box"}`
	s.files[key("remote", "boxyard/tombstones/20260601_rh9q4r.json")] = py
	got, err := Get(context.Background(), s, c, "remote", "20260601_rh9q4r")
	if err != nil {
		t.Fatal(err)
	}
	if got.DeletedByHostname != "Lukas’s MacBook Pro" {
		t.Errorf("hostname mangled: %q", got.DeletedByHostname)
	}
	at, err := got.DeletedAt()
	if err != nil || at.Year() != 2026 {
		t.Errorf("deleted_at parse: %v %v", at, err)
	}
}

// Pydantic omits the fraction on a whole second, so both forms appear on the
// remote and both must parse.
func TestDeletedAtParsesBothTimestampForms(t *testing.T) {
	for _, ts := range []string{"2026-08-22T21:30:00Z", "2026-08-22T21:30:00.123456Z"} {
		tomb := &Tombstone{BoxID: "b", DeletedAtUTC: ts, DeletedByHostname: "h", LastKnownName: "n"}
		if _, err := tomb.DeletedAt(); err != nil {
			t.Errorf("%q failed to parse: %v", ts, err)
		}
		if err := tomb.Validate(); err != nil {
			t.Errorf("%q failed validation: %v", ts, err)
		}
	}
	bad := &Tombstone{BoxID: "b", DeletedAtUTC: "yesterday", DeletedByHostname: "h", LastKnownName: "n"}
	if err := bad.Validate(); err == nil {
		t.Error("an unparseable deletion time was accepted")
	}
}

func TestListSkipsNonJSONAndDirectories(t *testing.T) {
	s, c := newFake(), cfg(t)
	dir := "boxyard/tombstones"
	s.entries[key("remote", dir)] = []Entry{
		{Name: "a.json"}, {Name: "notes.txt"}, {Name: "sub", IsDir: true}, {Name: "b.json"},
	}
	s.files[key("remote", dir+"/a.json")] = `{"box_id":"a","deleted_at_utc":"2026-08-22T21:30:00Z","deleted_by_hostname":"h","last_known_name":"na"}`
	s.files[key("remote", dir+"/b.json")] = `{"box_id":"b","deleted_at_utc":"2026-08-22T21:30:00Z","deleted_by_hostname":"h","last_known_name":"nb"}`

	got, err := List(context.Background(), s, c, "remote")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("expected 2 tombstones, got %d", len(got))
	}
}

// A fresh remote has no tombstones directory at all.
func TestListOnFreshRemoteIsEmptyNotAnError(t *testing.T) {
	s, c := newFake(), cfg(t)
	s.listNil = true
	got, err := List(context.Background(), s, c, "remote")
	if err != nil {
		t.Fatalf("a missing tombstones directory should not be an error: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("expected none, got %d", len(got))
	}
}

func TestRemove(t *testing.T) {
	s, c := newFake(), cfg(t)
	removed, err := Remove(context.Background(), s, c, "remote", "nope")
	if err != nil || removed {
		t.Fatalf("removing an absent tombstone: %v %v", removed, err)
	}
	if _, err := Create(context.Background(), s, c, "remote", "b1", "n"); err != nil {
		t.Fatal(err)
	}
	if removed, err = Remove(context.Background(), s, c, "remote", "b1"); err != nil || !removed {
		t.Fatalf("removing a present tombstone: %v %v", removed, err)
	}
	if ok, _ := IsTombstoned(context.Background(), s, c, "remote", "b1"); ok {
		t.Error("tombstone survived removal")
	}
}

func TestUnknownStorageLocationIsLoud(t *testing.T) {
	s, c := newFake(), cfg(t)
	if _, err := IsTombstoned(context.Background(), s, c, "nope", "b"); err == nil {
		t.Fatal("unknown storage location was accepted")
	}
}

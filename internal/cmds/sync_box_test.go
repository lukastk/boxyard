package cmds

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/remoteindex"
	"github.com/lukastk/boxyard/internal/storage"
	"github.com/lukastk/boxyard/internal/syncengine"
	"github.com/lukastk/boxyard/internal/tombstones"
)

// The real adapter must satisfy the whole SyncStore union. Asserted here rather
// than in internal/storage because SyncStore is declared in this package; a
// domain package growing a method then fails to build at the seam.
var _ SyncStore = (*storage.Adapter)(nil)

// fakeStore records what SyncBox asks of storage and answers from canned data.
// It is deliberately dumb: what is being tested is SyncBox's ORDERING and
// DECISIONS, not the state machine (which has its own 2400-scenario
// differential) or rclone.
type fakeStore struct {
	// dirs and files describe what exists, keyed "remote\x00path".
	dirs  map[string]bool
	files map[string]string

	syncCalls  []syncengine.SyncOptions
	catCalls   []string
	checkCalls int

	// checkAnswered/checkDiffering drive the ownership push probe.
	checkAnswered  bool
	checkDiffering []string
	checkErr       error

	lastModified time.Time
	haveModified bool
}

func newFakeStore() *fakeStore {
	return &fakeStore{dirs: map[string]bool{}, files: map[string]string{}, checkAnswered: true}
}

func fkey(remote, path string) string { return remote + "\x00" + path }

func (f *fakeStore) PathExists(_ context.Context, remote, p string) (bool, bool, error) {
	if f.dirs[fkey(remote, p)] {
		return true, true, nil
	}
	if _, ok := f.files[fkey(remote, p)]; ok {
		return true, false, nil
	}
	return false, false, nil
}

func (f *fakeStore) ReadSyncRecord(_ context.Context, remote, p string) (*models.SyncRecord, error) {
	content, ok := f.files[fkey(remote, p)]
	if !ok || content == "" {
		return nil, nil
	}
	rec, err := models.UnmarshalSyncRecord([]byte(content))
	if err != nil {
		return nil, err
	}
	return &rec, nil
}

func (f *fakeStore) LocalIsEmptyDir(string) (bool, error) { return false, nil }

func (f *fakeStore) LocalLastModified(string, map[string]bool) (time.Time, bool, error) {
	return f.lastModified, f.haveModified, nil
}

func (f *fakeStore) Mkdir(context.Context, string, string) error { return nil }

func (f *fakeStore) Sync(_ context.Context, o syncengine.SyncOptions) (bool, string, string, error) {
	f.syncCalls = append(f.syncCalls, o)
	return true, "", "", nil
}

func (f *fakeStore) Purge(context.Context, string, string) error { return nil }

func (f *fakeStore) WriteSyncRecord(_ context.Context, remote, p string, rec models.SyncRecord) error {
	data, err := rec.Marshal()
	if err != nil {
		return err
	}
	f.files[fkey(remote, p)] = string(data)
	return nil
}

func (f *fakeStore) Write(_ context.Context, remote, p, content string) error {
	f.files[fkey(remote, p)] = content
	return nil
}

func (f *fakeStore) Cat(_ context.Context, remote, p string) (bool, string, error) {
	f.catCalls = append(f.catCalls, p)
	content, ok := f.files[fkey(remote, p)]
	return ok, content, nil
}

func (f *fakeStore) Delete(_ context.Context, remote, p string) error {
	delete(f.files, fkey(remote, p))
	return nil
}

func (f *fakeStore) Check(_ context.Context, _, _ rclone.Location, _ rclone.TransferOptions) (bool, []string, error) {
	f.checkCalls++
	return f.checkAnswered, f.checkDiffering, f.checkErr
}

// ListJSON answers for the tombstones store. A nil slice means "no such
// directory", which is what a fresh remote looks like.
func (f *fakeStore) ListJSON(context.Context, string, string) ([]tombstones.Entry, error) {
	return nil, nil
}

// fakeIndexStore re-types the same listing for remoteindex, whose Entry is a
// distinct type. The real adapter does exactly this.
type fakeIndexStore struct{ *fakeStore }

func (f fakeIndexStore) ListJSON(context.Context, string, string) ([]remoteindex.Entry, error) {
	return nil, nil
}

func (f *fakeStore) ForRemoteIndex() remoteindex.Store { return fakeIndexStore{f} }

type nopPerms struct{}

func (nopPerms) Generate(string) (bool, error)  { return false, nil }
func (nopPerms) Apply(string) ([]string, error) { return nil, nil }

// remoteYard adds an rclone-typed storage location to a test yard.
func remoteYard(t *testing.T) *config.Config {
	t.Helper()
	cfg := newTestYard(t)
	cfg.StorageLocations["remote"] = &config.StorageConfig{
		StorageType: config.StorageRclone,
		StorePath:   "boxyard",
	}
	return cfg
}

func TestSyncBoxLocalStorageNeedsNoRemote(t *testing.T) {
	cfg := newTestYard(t)
	indexName, err := NewBox(cfg, NewBoxOptions{BoxName: "local-sync", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}

	s := newFakeStore()
	got, err := SyncBox(context.Background(), cfg, s, nopPerms{}, SyncBoxOptions{BoxIndexName: indexName})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != len(enums.AllBoxParts) {
		t.Fatalf("got %d parts, want one per part", len(got))
	}
	for part, res := range got {
		if res.Status.Condition != syncengine.LocalStorage {
			t.Errorf("%s: %q, want local_storage", part, res.Status.Condition)
		}
		if res.Synced {
			t.Errorf("%s: reported a transfer", part)
		}
	}
	if len(s.syncCalls) != 0 {
		t.Fatalf("a local storage location must not be transferred to: %+v", s.syncCalls)
	}
}

func TestSyncBoxUnknownBox(t *testing.T) {
	cfg := remoteYard(t)
	_, err := SyncBox(context.Background(), cfg, newFakeStore(), nopPerms{},
		SyncBoxOptions{BoxIndexName: "20240102_aaaaa__nope"})
	if err == nil || !strings.Contains(err.Error(), "not found") {
		t.Fatalf("want a not-found error, got %v", err)
	}
}

func TestSyncBoxTombstonedIsSkippedWithoutAProbe(t *testing.T) {
	cfg := remoteYard(t)
	indexName, err := NewBox(cfg, NewBoxOptions{BoxName: "gone", StorageLocation: "remote", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	boxID, err := models.ExtractBoxID(indexName)
	if err != nil {
		t.Fatal(err)
	}

	s := newFakeStore()
	got, err := SyncBox(context.Background(), cfg, s, nopPerms{}, SyncBoxOptions{
		BoxIndexName:     indexName,
		TombstonedBoxIDs: map[string]bool{boxID: true},
	})
	if err != nil {
		t.Fatal(err)
	}
	for part, res := range got {
		if res.Status.Condition != syncengine.Tombstoned {
			t.Errorf("%s: %q, want tombstoned", part, res.Status.Condition)
		}
	}
	if len(s.syncCalls) != 0 {
		t.Fatalf("a tombstoned box must not be synced: %+v", s.syncCalls)
	}
}

// setUpNeedsPush arranges the fake so every part of `bm` reads as NEEDS_PUSH:
// both sides present, matching complete sync records, and a local modification
// newer than the record.
func setUpNeedsPush(t *testing.T, cfg *config.Config, s *fakeStore, bm *models.BoxMeta, storePath string) {
	t.Helper()
	record := models.NewSyncRecord(true, "somewhere")
	data, err := record.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	recordTime, err := record.Time()
	if err != nil {
		t.Fatal(err)
	}
	s.lastModified = recordTime.Add(time.Hour)
	s.haveModified = true

	for _, part := range enums.AllBoxParts {
		localPath, err := bm.LocalPartPath(cfg, part)
		if err != nil {
			t.Fatal(err)
		}
		s.dirs[fkey("", localPath)] = true
		s.dirs[fkey("remote", remotePartPath(storePath, bm.IndexName(), part))] = true
		s.files[fkey("", bm.LocalSyncRecordPath(cfg, part))] = string(data)
		s.files[fkey("remote", remoteSyncRecordPath(storePath, bm.IndexName(), part))] = string(data)
	}
}

func ownedBox(t *testing.T, cfg *config.Config, name, owner string) *models.BoxMeta {
	t.Helper()
	indexName, err := NewBox(cfg, NewBoxOptions{BoxName: name, StorageLocation: "remote", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	meta, err := models.GetBoxyardMeta(cfg, true)
	if err != nil {
		t.Fatal(err)
	}
	bm := meta.ByIndexName()[indexName]
	bm.WriteOwner = owner
	if err := bm.Save(cfg); err != nil {
		t.Fatal(err)
	}
	return bm
}

// A non-owner holding real local changes must NOT push. This is the single
// most important property of the ownership model: the whole point is that two
// machines never both write the same box's DATA.
func TestSyncBoxNonOwnerDoesNotPush(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "theirs", "mymain")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")
	// A push WOULD move something: the probe says the changes are real.
	s.checkAnswered = true
	s.checkDiffering = []string{"notes.md"}

	got, err := SyncBox(context.Background(), cfg, s, nopPerms{}, SyncBoxOptions{
		BoxIndexName:     bm.IndexName(),
		Choices:          []enums.BoxPart{enums.PartData},
		TombstonedBoxIDs: map[string]bool{},
	})
	if err != nil {
		t.Fatal(err)
	}
	res := got[enums.PartData]
	if res.Status.Condition != syncengine.WriteDenied {
		t.Fatalf("DATA condition = %q, want write_denied", res.Status.Condition)
	}
	if res.Synced {
		t.Fatal("a non-owner reported a transfer")
	}
	if !strings.Contains(res.Status.ErrorMessage, "mymain") {
		t.Errorf("the message must name the owner: %q", res.Status.ErrorMessage)
	}
	// META is deliberately still writable by every machine — without that,
	// ownership could never be transferred. So the only thing that must not
	// have moved is DATA.
	for _, call := range s.syncCalls {
		if strings.HasSuffix(call.SourcePath, string(enums.PartData)) && call.Source == "" {
			t.Fatalf("a non-owner pushed DATA: %+v", call)
		}
	}
}

// NEEDS_PUSH is not evidence of a real change: it comes from a tree walk, so a
// single .DS_Store sets it even though the file can never be transferred. A
// non-owner whose "changes" would move nothing must report SYNCED, not a
// refusal — otherwise every read-only machine carries a permanent complaint
// about changes that do not exist.
func TestSyncBoxNonOwnerWithNothingToPushReportsSynced(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "theirs-clean", "mymain")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")
	s.checkAnswered = true
	s.checkDiffering = nil // a push would move nothing

	got, err := SyncBox(context.Background(), cfg, s, nopPerms{}, SyncBoxOptions{
		BoxIndexName:     bm.IndexName(),
		Choices:          []enums.BoxPart{enums.PartData},
		TombstonedBoxIDs: map[string]bool{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got[enums.PartData].Status.Condition != syncengine.Synced {
		t.Fatalf("condition = %q, want synced", got[enums.PartData].Status.Condition)
	}
	if s.checkCalls == 0 {
		t.Fatal("the push probe was never run")
	}
}

// An unanswerable probe must refuse, not wave the push through: the one thing
// this must never do is call a box clean because it failed to look.
func TestSyncBoxNonOwnerRefusesWhenTheProbeCannotAnswer(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "theirs-unknown", "mymain")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")
	s.checkAnswered = false

	got, err := SyncBox(context.Background(), cfg, s, nopPerms{}, SyncBoxOptions{
		BoxIndexName:     bm.IndexName(),
		Choices:          []enums.BoxPart{enums.PartData},
		TombstonedBoxIDs: map[string]bool{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got[enums.PartData].Status.Condition != syncengine.WriteDenied {
		t.Fatalf("condition = %q, want write_denied", got[enums.PartData].Status.Condition)
	}
}

// The owner pushes normally, and pays nothing for the probe.
func TestSyncBoxOwnerPushes(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "ours", "macbook")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")

	got, err := SyncBox(context.Background(), cfg, s, nopPerms{}, SyncBoxOptions{
		BoxIndexName:     bm.IndexName(),
		Choices:          []enums.BoxPart{enums.PartData},
		TombstonedBoxIDs: map[string]bool{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !got[enums.PartData].Synced {
		t.Fatalf("the owner did not push: %+v", got[enums.PartData].Status)
	}
	if s.checkCalls != 0 {
		t.Errorf("the owner paid for %d push probes; it should pay for none", s.checkCalls)
	}
}

// An UNOWNED box is unrestricted — ownership is opt-in per box, and mass
// -assigning it to hundreds of boxes nobody claimed is exactly what the model
// avoids.
func TestSyncBoxUnownedPushes(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "nobodys", "")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")

	got, err := SyncBox(context.Background(), cfg, s, nopPerms{}, SyncBoxOptions{
		BoxIndexName:     bm.IndexName(),
		Choices:          []enums.BoxPart{enums.PartData},
		TombstonedBoxIDs: map[string]bool{},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !got[enums.PartData].Synced {
		t.Fatalf("an unowned box was not pushed: %+v", got[enums.PartData].Status)
	}
}

// META and CONF are SYNCED whenever DATA is, even when the caller asked only
// for DATA — but their results are not REPORTED.
//
//   - META, because the ownership decision reads write_owner out of the
//     boxmeta; deciding it from a copy that predates another machine's claim is
//     the one path by which a non-owner could push without learning it had been
//     claimed.
//   - CONF, because conf/.rclone_include|_exclude|_filters decide WHAT DATA
//     syncs and are read off the local disk immediately before DATA moves. A
//     box whose .rclone_include narrows its sync would otherwise sync
//     EVERYTHING on a machine that had never pulled the conf (Python v0.5.5).
func TestSyncBoxMetaAndConfFollowData(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "follows", "macbook")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")

	got, err := SyncBox(context.Background(), cfg, s, nopPerms{}, SyncBoxOptions{
		BoxIndexName:     bm.IndexName(),
		Choices:          []enums.BoxPart{enums.PartData},
		TombstonedBoxIDs: map[string]bool{},
	})
	if err != nil {
		t.Fatal(err)
	}

	// The report answers exactly what was asked.
	if len(got) != 1 || got[enums.PartData].Status.Condition == "" {
		t.Fatalf("the result must contain DATA and nothing else, got %v", got)
	}

	// ...but all three parts moved.
	moved := map[string]bool{}
	for _, call := range s.syncCalls {
		for _, part := range enums.AllBoxParts {
			if strings.Contains(call.SourcePath, remotePartPath("boxyard", bm.IndexName(), part)) ||
				strings.Contains(call.DestPath, remotePartPath("boxyard", bm.IndexName(), part)) {
				moved[string(part)] = true
			}
		}
	}
	for _, part := range enums.AllBoxParts {
		if !moved[string(part)] {
			t.Errorf("%s was not synced even though DATA was; calls: %+v", part, s.syncCalls)
		}
	}
}

// A DATA sync must be filtered by the box's OWN exclude file when it has one,
// and by the global default otherwise. A per-box file REPLACES the global one,
// so using the wrong one can prune a directory the box really does sync.
func TestSyncBoxUsesTheBoxsOwnFilters(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "filtered", "macbook")

	confPath, err := bm.LocalPartPath(cfg, enums.PartConf)
	if err != nil {
		t.Fatal(err)
	}
	boxExclude := filepath.Join(confPath, boxconst.RcloneExcludeFilename)
	if err := os.WriteFile(boxExclude, []byte("secrets/\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")

	if _, err := SyncBox(context.Background(), cfg, s, nopPerms{}, SyncBoxOptions{
		BoxIndexName:     bm.IndexName(),
		Choices:          []enums.BoxPart{enums.PartData},
		TombstonedBoxIDs: map[string]bool{},
	}); err != nil {
		t.Fatal(err)
	}

	var dataCall *syncengine.SyncOptions
	for i, call := range s.syncCalls {
		if strings.HasSuffix(call.SourcePath, string(enums.PartData)) ||
			strings.HasSuffix(call.DestPath, string(enums.PartData)) {
			dataCall = &s.syncCalls[i]
		}
	}
	if dataCall == nil {
		t.Fatalf("DATA was never synced: %+v", s.syncCalls)
	}
	if dataCall.ExcludeFile != boxExclude {
		t.Fatalf("DATA synced with %q, want the box's own exclude file %q",
			dataCall.ExcludeFile, boxExclude)
	}
}

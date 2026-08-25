package cmds

import (
	"context"
	"errors"
	"os"
	"path"
	"path/filepath"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/ownership"
	"github.com/lukastk/boxyard/internal/rclone"
	"github.com/lukastk/boxyard/internal/remoteindex"
)

// moveRecorder is a fakeStore that also records remote renames.
type moveRecorder struct {
	*fakeStore
	moves [][2]string
	fail  bool
}

func (m *moveRecorder) Moveto(_ context.Context, src, dst rclone.Location) (rclone.Output, error) {
	m.moves = append(m.moves, [2]string{src.Path, dst.Path})
	if m.fail {
		return rclone.Output{OK: false, Stderr: "directory not found"}, nil
	}
	return rclone.Output{OK: true}, nil
}

func newMoveRecorder() *moveRecorder { return &moveRecorder{fakeStore: newFakeStore()} }

func TestRenameBoxLocalMovesEveryDirectory(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "old-name", "")
	oldIndex := bm.IndexName()

	// A sync record directory, which must move with the box.
	recordDir := filepath.Join(cfg.BoxyardDataPath, boxconst.SyncRecordsRelPath, oldIndex)
	if err := os.MkdirAll(recordDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(recordDir, "data.rec"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}

	s := newMoveRecorder()
	newIndex, err := RenameBox(context.Background(), cfg, s, RenameBoxOptions{
		BoxIndexName: oldIndex, NewName: "new-name", Scope: enums.RenameLocal,
	})
	if err != nil {
		t.Fatal(err)
	}
	// The box ID survives a rename; only the name half changes.
	if !strings.HasSuffix(newIndex, "__new-name") || !strings.HasPrefix(newIndex, bm.BoxID()) {
		t.Fatalf("new index name = %q", newIndex)
	}

	for _, p := range []string{
		filepath.Join(cfg.LocalStorePath(), "remote", newIndex),
		filepath.Join(cfg.UserBoxesPath, newIndex),
		filepath.Join(cfg.BoxyardDataPath, boxconst.SyncRecordsRelPath, newIndex, "data.rec"),
	} {
		if _, err := os.Stat(p); err != nil {
			t.Errorf("missing after rename: %s (%v)", p, err)
		}
	}
	for _, p := range []string{
		filepath.Join(cfg.LocalStorePath(), "remote", oldIndex),
		filepath.Join(cfg.UserBoxesPath, oldIndex),
	} {
		if _, err := os.Lstat(p); !os.IsNotExist(err) {
			t.Errorf("still present under the old name: %s", p)
		}
	}
	// The boxmeta must carry the new name, and be where the new index name says.
	onDisk, err := models.LoadBoxMeta(cfg, "remote", newIndex)
	if err != nil {
		t.Fatal(err)
	}
	if onDisk.Name != "new-name" {
		t.Fatalf("boxmeta name = %q", onDisk.Name)
	}
	// A LOCAL rename must not touch the remote.
	if len(s.moves) != 0 {
		t.Fatalf("a local rename reached the remote: %v", s.moves)
	}
}

// A REMOTE rename is a write to shared state, so a box another machine owns is
// gated. A LOCAL one is not: a read-only replica may call its own copy whatever
// it likes.
func TestRenameBoxOwnershipGate(t *testing.T) {
	cases := []struct {
		scope   enums.RenameScope
		refused bool
	}{
		{enums.RenameLocal, false},
		{enums.RenameRemote, true},
		{enums.RenameBoth, true},
	}
	for _, tc := range cases {
		t.Run(string(tc.scope), func(t *testing.T) {
			cfg := remoteYard(t)
			cfg.MachineName = "macbook"
			bm := ownedBox(t, cfg, "theirs", "mymain")

			s := newMoveRecorder()
			_, err := RenameBox(context.Background(), cfg, s, RenameBoxOptions{
				BoxIndexName: bm.IndexName(), NewName: "renamed", Scope: tc.scope,
			})
			var refused *ownership.RefusedError
			if tc.refused != errors.As(err, &refused) {
				t.Fatalf("scope %s: refused=%v, err=%v", tc.scope, errors.As(err, &refused), err)
			}
		})
	}
}

func TestRenameBoxRejectsABadName(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "fine", "")
	_, err := RenameBox(context.Background(), cfg, newMoveRecorder(), RenameBoxOptions{
		BoxIndexName: bm.IndexName(), NewName: "a/b", Scope: enums.RenameLocal,
	})
	if err == nil || !strings.Contains(err.Error(), "single path component") {
		t.Fatalf("want a name refusal, got %v", err)
	}
}

func TestRenameBoxRemoteMovesBoxAndRecords(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "old-name", "")
	oldIndex := bm.IndexName()

	s := newMoveRecorder()
	// The remote index cache is what says where the box lives remotely.
	if err := writeRemoteIndex(s.fakeStore, cfg, "remote", bm.BoxID(), oldIndex); err != nil {
		t.Fatal(err)
	}

	newIndex, err := RenameBox(context.Background(), cfg, s, RenameBoxOptions{
		BoxIndexName: oldIndex, NewName: "new-name", Scope: enums.RenameRemote,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(s.moves) != 2 {
		t.Fatalf("expected the box directory AND its sync records to move, got %v", s.moves)
	}
	wantBox := [2]string{
		path.Join("boxyard", boxconst.RemoteBoxesRelPath, oldIndex),
		path.Join("boxyard", boxconst.RemoteBoxesRelPath, newIndex),
	}
	if s.moves[0] != wantBox {
		t.Errorf("box move = %v, want %v", s.moves[0], wantBox)
	}
}

// A failure to move the SYNC RECORDS is a warning, not a failure: they are a
// cache the next sync rebuilds, and losing the box's directory rename halfway
// would be far worse.
func TestRenameBoxRemoteRecordFailureIsAWarning(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "old-name", "")
	s := &failSecondMove{moveRecorder: newMoveRecorder()}
	if err := writeRemoteIndex(s.fakeStore, cfg, "remote", bm.BoxID(), bm.IndexName()); err != nil {
		t.Fatal(err)
	}

	var out strings.Builder
	if _, err := RenameBox(context.Background(), cfg, s, RenameBoxOptions{
		BoxIndexName: bm.IndexName(), NewName: "new-name",
		Scope: enums.RenameRemote, Verbose: true, Out: &out,
	}); err != nil {
		t.Fatalf("a sync-record move failure must not fail the rename: %v", err)
	}
	if !strings.Contains(out.String(), "Failed to rename remote sync records") {
		t.Errorf("no warning was printed: %q", out.String())
	}
}

// ...but a failure to move the BOX is fatal.
func TestRenameBoxRemoteBoxFailureIsFatal(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "old-name", "")
	s := newMoveRecorder()
	if err := writeRemoteIndex(s.fakeStore, cfg, "remote", bm.BoxID(), bm.IndexName()); err != nil {
		t.Fatal(err)
	}
	s.fail = true
	if _, err := RenameBox(context.Background(), cfg, s, RenameBoxOptions{
		BoxIndexName: bm.IndexName(), NewName: "new-name", Scope: enums.RenameRemote,
	}); err == nil {
		t.Fatal("a failed box rename must be fatal")
	}
}

type failSecondMove struct {
	*moveRecorder
	calls int
}

func (f *failSecondMove) Moveto(ctx context.Context, src, dst rclone.Location) (rclone.Output, error) {
	f.calls++
	if f.calls == 2 {
		return rclone.Output{OK: false, Stderr: "no such directory"}, nil
	}
	return f.moveRecorder.Moveto(ctx, src, dst)
}

// writeRemoteIndex seeds the remote-index cache AND the remote path it points
// at. Find verifies the cached name still exists before trusting it — a cache
// entry alone proves nothing, which is exactly what makes it safe after a
// rename elsewhere.
func writeRemoteIndex(s *fakeStore, cfg *config.Config, storageLocation, boxID, indexName string) error {
	s.dirs[fkey(storageLocation, path.Join("boxyard", boxconst.RemoteBoxesRelPath, indexName))] = true
	return remoteindex.Update(cfg, storageLocation, boxID, indexName)
}

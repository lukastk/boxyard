package cmds

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/ownership"
	"github.com/lukastk/boxyard/internal/rclone"
)

// copyStore adds the plain copy verbs to the fake.
type copyStore struct {
	*fakeStore
	copies   [][2]string
	copytos  [][2]string
	copyFail bool
}

func (c *copyStore) Copy(_ context.Context, src, dst rclone.Location, _ rclone.TransferOptions) (rclone.Output, error) {
	c.copies = append(c.copies, [2]string{src.Path, dst.Path})
	return rclone.Output{OK: !c.copyFail, Stderr: "nope"}, nil
}

func (c *copyStore) Copyto(_ context.Context, src, dst rclone.Location, _ rclone.CopytoOptions) (rclone.Output, error) {
	c.copytos = append(c.copytos, [2]string{src.Path, dst.Path})
	return rclone.Output{OK: !c.copyFail, Stderr: "nope"}, nil
}

func newCopyStore() *copyStore { return &copyStore{fakeStore: newFakeStore()} }

// Every path that writes to the remote is gated on ownership, INDEPENDENTLY of
// any force flag: a --force that also bypassed ownership would leave the remote
// holding this machine's data while boxmeta.toml still names another as owner —
// a lie in shared state, which is worse than a refusal.
func TestDestructiveCommandsAreOwnershipGated(t *testing.T) {
	t.Run("delete", func(t *testing.T) {
		cfg := remoteYard(t)
		cfg.MachineName = "macbook"
		bm := ownedBox(t, cfg, "theirs", "mymain")
		err := DeleteBox(context.Background(), cfg, newFakeStore(),
			DeleteBoxOptions{BoxIndexName: bm.IndexName()})
		var refused *ownership.RefusedError
		if !errors.As(err, &refused) {
			t.Fatalf("want a RefusedError, got %v", err)
		}
		// And the box must still be here.
		if _, err := os.Stat(bm.LocalPath(cfg)); err != nil {
			t.Fatalf("the box was removed despite the refusal: %v", err)
		}
	})

	t.Run("force-push", func(t *testing.T) {
		cfg := remoteYard(t)
		cfg.MachineName = "macbook"
		bm := ownedBox(t, cfg, "theirs", "mymain")
		src := t.TempDir()
		err := ForcePushToRemote(context.Background(), cfg, newFakeStore(), nopPerms{},
			ForcePushOptions{BoxIndexName: bm.IndexName(), SourcePath: src, Force: true})
		var refused *ownership.RefusedError
		if !errors.As(err, &refused) {
			t.Fatalf("want a RefusedError, got %v", err)
		}
	})
}

func TestForcePushRefusesWithoutForce(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "mine", "")
	err := ForcePushToRemote(context.Background(), cfg, newFakeStore(), nopPerms{},
		ForcePushOptions{BoxIndexName: bm.IndexName(), SourcePath: t.TempDir()})
	if err == nil || !strings.Contains(err.Error(), "--force") {
		t.Fatalf("want a refusal naming --force, got %v", err)
	}
}

func TestDeleteBoxRemovesEverything(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "doomed", "")
	indexName := bm.IndexName()

	// Sync records and backups, which must go too — a delete that leaves them
	// behind leaves the orphans doctor used to report.
	recordDir := filepath.Dir(bm.LocalSyncRecordPath(cfg, enums.PartData))
	if err := os.MkdirAll(recordDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(recordDir, "data.rec"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	backupDir := filepath.Join(cfg.LocalSyncBackupsPath(), indexName)
	if err := os.MkdirAll(backupDir, 0o755); err != nil {
		t.Fatal(err)
	}

	s := newFakeStore()
	if err := DeleteBox(context.Background(), cfg, s, DeleteBoxOptions{BoxIndexName: indexName}); err != nil {
		t.Fatal(err)
	}

	dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{dataPath, bm.LocalPath(cfg), recordDir, backupDir} {
		if _, err := os.Lstat(p); !os.IsNotExist(err) {
			t.Errorf("still present after delete: %s", p)
		}
	}
	// A TOMBSTONE must have been written, or another machine would resurrect
	// the box on its next sync.
	found := false
	for key := range s.files {
		if strings.Contains(key, "tombstones") && strings.Contains(key, bm.BoxID()) {
			found = true
		}
	}
	if !found {
		t.Fatalf("no tombstone was written: %v", keysOf(s.files))
	}
	if stillRegistered(t, cfg, indexName) {
		t.Error("the box is still registered")
	}
}

func TestCopyFromRemoteRefusesAManagedDestination(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "source", "")

	// Copying into the managed trees would create a directory that looks like a
	// box registration but is not one, which breaks the next registry refresh
	// for the WHOLE yard.
	for _, dest := range []string{
		filepath.Join(cfg.UserBoxesPath, "somewhere"),
		filepath.Join(cfg.BoxyardDataPath, "somewhere"),
	} {
		_, err := CopyFromRemote(context.Background(), cfg, newCopyStore(), nopPerms{},
			CopyFromRemoteOptions{BoxIndexName: bm.IndexName(), DestPath: dest})
		if err == nil || !strings.Contains(err.Error(), "not allowed") {
			t.Errorf("dest %s: want a refusal, got %v", dest, err)
		}
	}
}

func TestCopyFromRemoteRefusesAnExistingDestination(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "source", "")
	dest := t.TempDir() // exists

	_, err := CopyFromRemote(context.Background(), cfg, newCopyStore(), nopPerms{},
		CopyFromRemoteOptions{BoxIndexName: bm.IndexName(), DestPath: dest})
	if err == nil || !strings.Contains(err.Error(), "--overwrite") {
		t.Fatalf("want a refusal naming --overwrite, got %v", err)
	}
}

func TestCopyFromRemoteCopiesTheRequestedParts(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "source", "")
	dest := filepath.Join(t.TempDir(), "out")

	s := newCopyStore()
	if err := writeRemoteIndex(s.fakeStore, cfg, "remote", bm.BoxID(), bm.IndexName()); err != nil {
		t.Fatal(err)
	}

	if _, err := CopyFromRemote(context.Background(), cfg, s, nopPerms{}, CopyFromRemoteOptions{
		BoxIndexName: bm.IndexName(), DestPath: dest, CopyMeta: true, CopyConf: true,
	}); err != nil {
		t.Fatal(err)
	}
	// DATA and CONF are directories (copy); META is one file (copyto).
	if len(s.copies) != 2 {
		t.Fatalf("expected DATA and CONF copies, got %v", s.copies)
	}
	if len(s.copytos) != 1 || !strings.HasSuffix(s.copytos[0][0], boxconst.BoxMetafileRelPath) {
		t.Fatalf("META must be copied with copyto, got %v", s.copytos)
	}
}

func keysOf(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

// stillRegistered reports whether a box is still in the registry.
func stillRegistered(t *testing.T, cfg *config.Config, indexName string) bool {
	t.Helper()
	meta, err := models.GetBoxyardMeta(cfg, true)
	if err != nil {
		t.Fatal(err)
	}
	_, ok := meta.ByIndexName()[indexName]
	return ok
}

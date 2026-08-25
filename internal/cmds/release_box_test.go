package cmds

import (
	"context"
	"errors"
	"path"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/ownership"
)

// seedRemoteBoxMeta puts a boxmeta on the fake remote so the read-back can find
// one. `owner` empty means unowned.
func seedRemoteBoxMeta(s *fakeStore, storePath, indexName, owner string) {
	body := "storage_location = \"remote\"\ncreator_hostname = \"h\"\ngroups = []\nparents = []\n"
	if owner != "" {
		body += "write_owner = \"" + owner + "\"\n"
	}
	s.files[fkey("remote", path.Join(storePath, boxconst.RemoteBoxesRelPath,
		indexName, boxconst.BoxMetafileRelPath))] = body
}

func TestReleaseBoxOnAnUnownedBoxIsANoOp(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "free", "")

	s := newFakeStore()
	var out strings.Builder
	if err := ReleaseBox(context.Background(), cfg, s, nopPerms{}, ReleaseBoxOptions{
		BoxIndexName: bm.IndexName(), Verbose: true, Out: &out,
	}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "already unowned") {
		t.Fatalf("output = %q", out.String())
	}
}

func TestReleaseBoxRefusesAnotherMachinesBox(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "theirs", "mymain")

	s := newFakeStore()
	err := ReleaseBox(context.Background(), cfg, s, nopPerms{}, ReleaseBoxOptions{
		BoxIndexName: bm.IndexName(),
	})
	var refused *ownership.RefusedError
	if !errors.As(err, &refused) {
		t.Fatalf("want a RefusedError, got %v", err)
	}
	// The refusal must name BOTH ways out.
	for _, want := range []string{"mymain", "boxyard release", "claim --steal"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not mention %q: %s", want, err)
		}
	}
}

// The whole point of reading the remote back: a release that only happened
// locally is WORSE than no release, because every other machine still believes
// this one owns the box while this one believes it is free.
func TestReleaseBoxRollsBackWhenThePushDoesNotLand(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "stuck", "macbook")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")
	// The remote still shows the OLD owner: the push did not land.
	seedRemoteBoxMeta(s, "boxyard", bm.IndexName(), "macbook")

	err := ReleaseBox(context.Background(), cfg, s, nopPerms{}, ReleaseBoxOptions{
		BoxIndexName: bm.IndexName(),
	})
	if err == nil {
		t.Fatal("a release that did not reach the remote must fail")
	}
	if !strings.Contains(err.Error(), "still owns it") {
		t.Errorf("the error does not say this machine still owns the box: %s", err)
	}

	// And the LOCAL boxmeta must be back to how it was — `exclude` refuses to
	// drop a box it owns when the release cannot be published, and that refusal
	// is only honest if this holds.
	onDisk, err2 := models.LoadBoxMeta(cfg, "remote", bm.IndexName())
	if err2 != nil {
		t.Fatal(err2)
	}
	if onDisk.WriteOwner != "macbook" {
		t.Fatalf("write_owner = %q after a failed release; the rollback did not happen", onDisk.WriteOwner)
	}
}

func TestReleaseBoxSucceedsWhenTheRemoteShowsItCleared(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "handover", "macbook")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")
	// The remote agrees: unowned.
	seedRemoteBoxMeta(s, "boxyard", bm.IndexName(), "")

	var out strings.Builder
	if err := ReleaseBox(context.Background(), cfg, s, nopPerms{}, ReleaseBoxOptions{
		BoxIndexName: bm.IndexName(), Verbose: true, Out: &out,
	}); err != nil {
		t.Fatal(err)
	}
	onDisk, err := models.LoadBoxMeta(cfg, "remote", bm.IndexName())
	if err != nil {
		t.Fatal(err)
	}
	if onDisk.WriteOwner != "" {
		t.Fatalf("write_owner = %q, want empty", onDisk.WriteOwner)
	}
	if !strings.Contains(out.String(), "unowned again") {
		t.Errorf("output = %q", out.String())
	}
}

// SkipSync is for callers already inside a sync of this box. It must NOT read
// the remote back — there is nothing to read yet.
func TestReleaseBoxSkipSyncWritesLocallyOnly(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "inline", "macbook")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")

	if err := ReleaseBox(context.Background(), cfg, s, nopPerms{}, ReleaseBoxOptions{
		BoxIndexName: bm.IndexName(), SkipSync: true,
	}); err != nil {
		t.Fatal(err)
	}
	onDisk, err := models.LoadBoxMeta(cfg, "remote", bm.IndexName())
	if err != nil {
		t.Fatal(err)
	}
	if onDisk.WriteOwner != "" {
		t.Fatalf("write_owner = %q, want empty", onDisk.WriteOwner)
	}
	if len(s.catCalls) != 0 {
		t.Errorf("SkipSync read the remote back: %v", s.catCalls)
	}
}

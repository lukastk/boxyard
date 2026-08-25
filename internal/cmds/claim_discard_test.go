package cmds

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/ownership"
)

func TestClaimBoxRequiresAMachineName(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "" // an unnamed machine cannot be an owner
	bm := ownedBox(t, cfg, "unnamed", "")

	_, err := ClaimBox(context.Background(), cfg, newFakeStore(), nopPerms{},
		ClaimBoxOptions{BoxIndexName: bm.IndexName()})
	var refused *ownership.RefusedError
	if !errors.As(err, &refused) || !strings.Contains(err.Error(), "machine_name") {
		t.Fatalf("want a refusal naming the setting, got %v", err)
	}
}

// Claiming a box this machine does not hold would lock out every machine that
// does — this machine has no DATA to push.
func TestClaimBoxRefusesABoxNotIncludedHere(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "elsewhere", "")
	dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(dataPath); err != nil {
		t.Fatal(err)
	}

	_, err = ClaimBox(context.Background(), cfg, newFakeStore(), nopPerms{},
		ClaimBoxOptions{BoxIndexName: bm.IndexName()})
	if err == nil || !strings.Contains(err.Error(), "boxyard include") {
		t.Fatalf("the refusal must name the command that fixes it, got %v", err)
	}
}

func TestClaimBoxRefusesALocalStorageBox(t *testing.T) {
	cfg := newTestYard(t)
	cfg.MachineName = "macbook"
	indexName, err := NewBox(cfg, NewBoxOptions{BoxName: "local-only", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	// No other machine can reach a local storage location, so there is nothing
	// to coordinate.
	if _, err := ClaimBox(context.Background(), cfg, newFakeStore(), nopPerms{},
		ClaimBoxOptions{BoxIndexName: indexName}); err == nil ||
		!strings.Contains(err.Error(), "nothing to coordinate") {
		t.Fatalf("want a refusal, got %v", err)
	}
}

func TestClaimBoxRefusesAnotherMachinesBoxWithoutSteal(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "theirs", "mymain")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")
	seedRemoteBoxMeta(s, "boxyard", bm.IndexName(), "mymain")

	_, err := ClaimBox(context.Background(), cfg, s, nopPerms{},
		ClaimBoxOptions{BoxIndexName: bm.IndexName()})
	if err == nil {
		t.Fatal("expected a refusal")
	}
	// BOTH ways out must be named: the tidy handover and the steal.
	for _, want := range []string{"boxyard release", "claim --steal"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not offer %q: %s", want, err)
		}
	}
}

// The read-back is the whole reason this is a command and not a boxmeta edit:
// two machines claiming at the same instant is last-write-wins, so the only way
// to know whether the claim holds is to ask the remote.
func TestClaimBoxDetectsALostRace(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "contested", "")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")
	// Another machine got there first.
	seedRemoteBoxMeta(s, "boxyard", bm.IndexName(), "mymain")

	_, err := ClaimBox(context.Background(), cfg, s, nopPerms{},
		ClaimBoxOptions{BoxIndexName: bm.IndexName(), Steal: true})
	if err == nil || !strings.Contains(err.Error(), "did not stick") {
		t.Fatalf("want a lost-race refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "mymain") {
		t.Errorf("the message must name the machine that won: %s", err)
	}
}

func TestClaimBoxSucceedsWhenTheRemoteAgrees(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "mine-now", "")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")
	seedRemoteBoxMeta(s, "boxyard", bm.IndexName(), "macbook")

	var out strings.Builder
	owner, err := ClaimBox(context.Background(), cfg, s, nopPerms{}, ClaimBoxOptions{
		BoxIndexName: bm.IndexName(), Verbose: true, Out: &out,
	})
	if err != nil {
		t.Fatal(err)
	}
	if owner != "macbook" {
		t.Fatalf("owner = %q", owner)
	}
	onDisk, err := models.LoadBoxMeta(cfg, "remote", bm.IndexName())
	if err != nil {
		t.Fatal(err)
	}
	if onDisk.WriteOwner != "macbook" {
		t.Fatalf("write_owner = %q", onDisk.WriteOwner)
	}
	if !strings.Contains(out.String(), "Claimed") {
		t.Errorf("output = %q", out.String())
	}
}

func TestClaimBoxAlreadyOursIsANoOp(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "already", "macbook")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")

	var out strings.Builder
	if _, err := ClaimBox(context.Background(), cfg, s, nopPerms{}, ClaimBoxOptions{
		BoxIndexName: bm.IndexName(), Verbose: true, Out: &out,
	}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "already owned by this machine") {
		t.Errorf("output = %q", out.String())
	}
}

func TestDiscardLocalRefusals(t *testing.T) {
	t.Run("local storage location", func(t *testing.T) {
		cfg := newTestYard(t)
		indexName, err := NewBox(cfg, NewBoxOptions{BoxName: "local-only", InitialiseGit: false})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := DiscardLocal(context.Background(), cfg, newFakeStore(), nopPerms{},
			DiscardLocalOptions{BoxIndexName: indexName}); err == nil ||
			!strings.Contains(err.Error(), "no remote copy") {
			t.Fatalf("want a refusal, got %v", err)
		}
	})

	t.Run("not included here", func(t *testing.T) {
		cfg := remoteYard(t)
		bm := ownedBox(t, cfg, "absent", "")
		dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.RemoveAll(dataPath); err != nil {
			t.Fatal(err)
		}
		if _, err := DiscardLocal(context.Background(), cfg, newFakeStore(), nopPerms{},
			DiscardLocalOptions{BoxIndexName: bm.IndexName()}); err == nil ||
			!strings.Contains(err.Error(), "no local changes to discard") {
			t.Fatalf("want a refusal, got %v", err)
		}
	})
}

// What is discarded must be RECOVERABLE: this is one of the two ways out of
// WRITE_DENIED, and a way out that destroys work is one people refuse to take.
func TestDiscardLocalKeepsWhatItOverwrites(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "discarding", "")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")

	backups, err := DiscardLocal(context.Background(), cfg, s, nopPerms{},
		DiscardLocalOptions{BoxIndexName: bm.IndexName()})
	if err != nil {
		t.Fatal(err)
	}
	if backups != cfg.LocalSyncBackupsPath() {
		t.Fatalf("backups path = %q, want %q", backups, cfg.LocalSyncBackupsPath())
	}
	if len(s.syncCalls) == 0 {
		t.Fatal("no transfer happened")
	}
	for _, call := range s.syncCalls {
		if call.BackupPath == "" {
			t.Fatalf("a transfer ran with no backup path: %+v", call)
		}
	}
	// And the backup must SURVIVE. A backup directory that is purged straight
	// afterwards is no backup at all, and this is one of the two ways out of
	// WRITE_DENIED — a way out that destroys work is one people refuse to take.
	if len(s.purged) != 0 {
		t.Fatalf("the backup was purged: %v", s.purged)
	}
}

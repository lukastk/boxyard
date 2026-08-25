package ownership

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/rclone"
)

func box(owner string) *models.BoxMeta {
	return &models.BoxMeta{
		CreationTimestampUTC: "20240102",
		BoxSubid:             "aaaaa",
		Name:                 "a-box",
		WriteOwner:           owner,
	}
}

func TestMayPush(t *testing.T) {
	cases := []struct {
		name        string
		machineName string
		owner       string
		want        bool
	}{
		// Unowned means unrestricted: ownership is opt-in per box, and a box
		// nobody claimed is nobody's to refuse.
		{"unowned, named machine", "macbook", "", true},
		{"unowned, unnamed machine", "", "", true},
		{"owned by us", "macbook", "macbook", true},
		{"owned by another", "macbook", "mymain", false},
		// A machine that cannot say who it is must not be able to claim it is
		// the writer. The safe direction.
		{"owned, and we have no name", "", "mymain", false},
		// Names are compared exactly — no case folding, no trimming. A machine
		// that half-matches is not the owner.
		{"owner differs in case", "MacBook", "macbook", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := &config.Config{MachineName: tc.machineName}
			if got := MayPush(cfg, box(tc.owner)); got != tc.want {
				t.Fatalf("MayPush = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestRequireMachineName(t *testing.T) {
	cfg := &config.Config{MachineName: "macbook"}
	name, err := RequireMachineName(cfg, "claim a box")
	if err != nil || name != "macbook" {
		t.Fatalf("got (%q, %v)", name, err)
	}

	cfg = &config.Config{ConfigPath: "/etc/boxyard.toml"}
	_, err = RequireMachineName(cfg, "claim a box")
	var refused *RefusedError
	if !errors.As(err, &refused) {
		t.Fatalf("want a RefusedError, got %v", err)
	}
	// The refusal must name the file to edit — a refusal without the fix in it
	// is a refusal people work around.
	for _, want := range []string{"claim a box", "machine_name", "/etc/boxyard.toml"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not mention %q: %s", want, err)
		}
	}
}

func TestOwnerGate(t *testing.T) {
	cfg := &config.Config{MachineName: "macbook", BoxyardDataPath: "/data"}

	if err := OwnerGate(cfg, box(""), "delete"); err != nil {
		t.Fatalf("an unowned box must not be gated: %v", err)
	}
	if err := OwnerGate(cfg, box("macbook"), "delete"); err != nil {
		t.Fatalf("a box we own must not be gated: %v", err)
	}

	err := OwnerGate(cfg, box("mymain"), "delete")
	if err == nil {
		t.Fatal("a box another machine owns must be gated")
	}
	// BOTH ways out must always be named. A refusal with only one escape is a
	// refusal people work around.
	for _, want := range []string{"claim --steal", "discard-local", "release"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not offer %q: %s", want, err)
		}
	}
	// And it must never suggest something destructive: doctor's
	// `duplicate-box-id` hint once said "delete or re-create one of them", and
	// delete purges the remote and writes a tombstone keyed by box id — so
	// following it destroyed BOTH boxes.
	if strings.Contains(err.Error(), "boxyard delete") {
		t.Errorf("the refusal suggests a destructive command: %s", err)
	}
}

// checkStub stands in for rclone.
type checkStub struct {
	answered  bool
	differing []string
	err       error
	gotOpts   rclone.TransferOptions
	gotSrc    rclone.Location
	gotDst    rclone.Location
}

func (s *checkStub) Check(_ context.Context, src, dst rclone.Location, o rclone.TransferOptions) (bool, []string, error) {
	s.gotSrc, s.gotDst, s.gotOpts = src, dst, o
	return s.answered, s.differing, s.err
}

func TestPushWouldTransfer(t *testing.T) {
	ctx := context.Background()

	t.Run("nothing differs", func(t *testing.T) {
		s := &checkStub{answered: true}
		got, err := PushWouldTransfer(ctx, s, "/local", "hetzner", "boxyard/boxes/x/data", "", "/excl", "")
		if err != nil || got {
			t.Fatalf("got (%v, %v), want (false, nil)", got, err)
		}
		if s.gotSrc.Remote != "" || s.gotSrc.Path != "/local" {
			t.Errorf("source must be the LOCAL side: %+v", s.gotSrc)
		}
		if s.gotDst.Remote != "hetzner" {
			t.Errorf("destination must be the remote: %+v", s.gotDst)
		}
		if s.gotOpts.ExcludeFile != "/excl" {
			t.Errorf("the box's own filters must be passed through: %+v", s.gotOpts)
		}
	})

	t.Run("something differs", func(t *testing.T) {
		s := &checkStub{answered: true, differing: []string{"notes.md"}}
		got, err := PushWouldTransfer(ctx, s, "/local", "hetzner", "p", "", "", "")
		if err != nil || !got {
			t.Fatalf("got (%v, %v), want (true, nil)", got, err)
		}
	})

	// The one thing this must never do is claim a box is clean because it
	// failed to look. An unanswerable comparison is reported as "would
	// transfer", so the box surfaces as WRITE_DENIED rather than as SYNCED.
	t.Run("unanswerable is not clean", func(t *testing.T) {
		s := &checkStub{answered: false}
		got, err := PushWouldTransfer(ctx, s, "/local", "hetzner", "p", "", "", "")
		if err != nil || !got {
			t.Fatalf("got (%v, %v), want (true, nil)", got, err)
		}
	})

	t.Run("an error is not clean either", func(t *testing.T) {
		s := &checkStub{err: errors.New("rclone did not run")}
		got, err := PushWouldTransfer(ctx, s, "/local", "hetzner", "p", "", "", "")
		if err == nil {
			t.Fatal("the error must be surfaced")
		}
		if !got {
			t.Fatal("an error must not be reported as 'nothing to transfer'")
		}
	})
}

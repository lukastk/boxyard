package cmds

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
)

func TestIncludeBoxRefusesAnAlreadyIncludedBox(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "here", "")
	err := IncludeBox(context.Background(), cfg, newFakeStore(), nopPerms{},
		IncludeBoxOptions{BoxIndexName: bm.IndexName()})
	if err == nil || !strings.Contains(err.Error(), "already included") {
		t.Fatalf("want an already-included refusal, got %v", err)
	}
}

func TestIncludeBoxUnknownBox(t *testing.T) {
	cfg := remoteYard(t)
	err := IncludeBox(context.Background(), cfg, newFakeStore(), nopPerms{},
		IncludeBoxOptions{BoxIndexName: "20240102_aaaaa__nope"})
	if err == nil || !strings.Contains(err.Error(), "does not exist") {
		t.Fatalf("want a not-found error, got %v", err)
	}
}

// Including a box says what it means for WRITING to it, and the message is read
// from DISK after the syncs — they may have pulled a boxmeta naming an owner
// this machine did not know about a moment ago.
func TestIncludeBoxReportsOwnership(t *testing.T) {
	cases := []struct {
		name     string
		owner    string
		readOnly bool
		want     string
		absent   string
	}{
		{"unowned nudges a claim", "", false, "has no write owner", ""},
		{"read-only suppresses the nudge", "", true, "", "has no write owner"},
		{"another machine's box says read-only", "mymain", false, "Included read-only", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := remoteYard(t)
			cfg.MachineName = "macbook"
			bm := ownedBox(t, cfg, "arriving", tc.owner)

			s := newFakeStore()
			// Both sides in step, so the syncs are no-ops and what is being
			// tested is the MESSAGE, not the transfer.
			setUpNeedsPush(t, cfg, s, bm, "boxyard")
			// Older than the sync record, so nothing reads as modified and the
			// syncs are no-ops: what is being tested is the MESSAGE.
			s.lastModified = time.Unix(0, 0)

			// Make it look NOT included: the real DATA directory is what
			// CheckIncluded consults.
			dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.RemoveAll(dataPath); err != nil {
				t.Fatal(err)
			}

			var out strings.Builder
			if err := IncludeBox(context.Background(), cfg, s, nopPerms{}, IncludeBoxOptions{
				BoxIndexName: bm.IndexName(), ReadOnly: tc.readOnly, Out: &out,
			}); err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(out.String(), "Included box 'arriving'") {
				t.Errorf("no inclusion line: %q", out.String())
			}
			if tc.want != "" && !strings.Contains(out.String(), tc.want) {
				t.Errorf("output %q does not mention %q", out.String(), tc.want)
			}
			if tc.absent != "" && strings.Contains(out.String(), tc.absent) {
				t.Errorf("output %q should not mention %q", out.String(), tc.absent)
			}
		})
	}
}

func TestExcludeBoxRefusesALocalStorageBox(t *testing.T) {
	cfg := newTestYard(t)
	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "local-only", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	// A local storage location IS the local copy: excluding would delete the
	// only copy there is.
	err = ExcludeBox(context.Background(), cfg, newFakeStore(), nopPerms{},
		ExcludeBoxOptions{BoxIndexName: indexName})
	if err == nil || !strings.Contains(err.Error(), "cannot be excluded") {
		t.Fatalf("want a refusal, got %v", err)
	}
}

func TestExcludeBoxRefusesAnAlreadyExcludedBox(t *testing.T) {
	cfg := remoteYard(t)
	bm := ownedBox(t, cfg, "gone-already", "")
	dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(dataPath); err != nil {
		t.Fatal(err)
	}
	err = ExcludeBox(context.Background(), cfg, newFakeStore(), nopPerms{},
		ExcludeBoxOptions{BoxIndexName: bm.IndexName()})
	if err == nil || !strings.Contains(err.Error(), "already excluded") {
		t.Fatalf("want an already-excluded refusal, got %v", err)
	}
}

func TestExcludeBoxRemovesDataAndItsSyncRecord(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "leaving", "")

	dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataPath, "file.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	recordPath := bm.LocalSyncRecordPath(cfg, enums.PartData)
	if err := os.MkdirAll(filepath.Dir(recordPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(recordPath, []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}

	var out strings.Builder
	if err := ExcludeBox(context.Background(), cfg, newFakeStore(), nopPerms{}, ExcludeBoxOptions{
		BoxIndexName: bm.IndexName(), SkipSync: true, Out: &out,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(dataPath); !os.IsNotExist(err) {
		t.Error("the DATA directory survived")
	}
	// The record has to go too, or the next status probe sees a record with no
	// data and calls it an error.
	if _, err := os.Stat(recordPath); !os.IsNotExist(err) {
		t.Error("the DATA sync record survived")
	}
	if !strings.Contains(out.String(), "Excluded box 'leaving'") {
		t.Errorf("output = %q", out.String())
	}
}

// A box THIS machine owns must have its ownership released as part of the same
// operation. Excluding without it leaves boxmeta.toml naming a machine that no
// longer has the DATA — which no machine could then push.
func TestExcludeBoxReleasesOwnershipFirst(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "mine", "macbook")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")
	seedRemoteBoxMeta(s, "boxyard", bm.IndexName(), "") // the release lands

	var out strings.Builder
	if err := ExcludeBox(context.Background(), cfg, s, nopPerms{}, ExcludeBoxOptions{
		BoxIndexName: bm.IndexName(), SkipSync: true, Out: &out,
	}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Released write ownership") {
		t.Errorf("no release line: %q", out.String())
	}
}

// ...and if the release cannot be published, exclude REFUSES rather than
// leaving a stale owner behind.
func TestExcludeBoxRefusesWhenTheReleaseCannotBePublished(t *testing.T) {
	cfg := remoteYard(t)
	cfg.MachineName = "macbook"
	bm := ownedBox(t, cfg, "stuck-mine", "macbook")

	s := newFakeStore()
	setUpNeedsPush(t, cfg, s, bm, "boxyard")
	seedRemoteBoxMeta(s, "boxyard", bm.IndexName(), "macbook") // the release did NOT land

	err := ExcludeBox(context.Background(), cfg, s, nopPerms{}, ExcludeBoxOptions{
		BoxIndexName: bm.IndexName(), SkipSync: true,
	})
	if err == nil || !strings.Contains(err.Error(), "Cannot exclude") {
		t.Fatalf("want a refusal, got %v", err)
	}
	// The DATA must still be here: a refusal that deleted the data anyway would
	// be the worst of both.
	dataPath, err2 := bm.LocalPartPath(cfg, enums.PartData)
	if err2 != nil {
		t.Fatal(err2)
	}
	if _, statErr := os.Stat(dataPath); statErr != nil {
		t.Fatalf("the DATA was removed despite the refusal: %v", statErr)
	}
	onDisk, err2 := models.LoadBoxMeta(cfg, "remote", bm.IndexName())
	if err2 != nil {
		t.Fatal(err2)
	}
	if onDisk.WriteOwner != "macbook" {
		t.Fatalf("write_owner = %q; the failed release was not rolled back", onDisk.WriteOwner)
	}
}

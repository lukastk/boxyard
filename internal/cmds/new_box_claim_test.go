package cmds

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/models"
)

// `NewBox` never set WriteOwner, so every box created since ownership landed
// in v0.5.2 was born UNOWNED — the exact state the feature exists to remove.
// On mymain the only unowned boxes held there were the three created since the
// claim sweep.
//
// The "unowned by default" rule was a MIGRATION guarantee — v0.5.2 promised
// "nothing changes for the 583 boxes in the yard until someone claims them" —
// and a box created afterwards has no v0.4.x behaviour to preserve.

func boolPtr(b bool) *bool { return &b }

func TestNewBoxClaimsForThisMachine(t *testing.T) {
	cfg := newTestYard(t)
	cfg.MachineName = "testmachine"

	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "mine"})
	if err != nil {
		t.Fatal(err)
	}
	bm, err := models.LoadBoxMeta(cfg, "local", indexName)
	if err != nil {
		t.Fatal(err)
	}
	if bm.WriteOwner != "testmachine" {
		t.Errorf("write_owner = %q, want the creating machine", bm.WriteOwner)
	}
}

func TestNewBoxNoClaimLeavesItUnowned(t *testing.T) {
	cfg := newTestYard(t)
	cfg.MachineName = "testmachine"

	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{
		BoxName: "theirs", Claim: boolPtr(false),
	})
	if err != nil {
		t.Fatal(err)
	}
	bm, err := models.LoadBoxMeta(cfg, "local", indexName)
	if err != nil {
		t.Fatal(err)
	}
	if bm.WriteOwner != "" {
		t.Errorf("write_owner = %q, want unowned", bm.WriteOwner)
	}
	// ABSENT, not empty: an unowned box omits the key entirely, which is what
	// keeps every pre-0.5 boxmeta byte-identical.
	rawBytes, err := os.ReadFile(filepath.Join(cfg.LocalStorePath(), "local", indexName, "boxmeta.toml"))
	if err != nil {
		t.Fatal(err)
	}
	if raw := string(rawBytes); strings.Contains(raw, "write_owner") {
		t.Errorf("an unowned box wrote the key:\n%s", raw)
	}
}

func TestNewBoxWithoutAMachineNameStillCreatesTheBox(t *testing.T) {
	cfg := newTestYard(t)
	cfg.MachineName = ""

	var out bytes.Buffer
	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{
		BoxName: "nameless", Out: &out,
	})
	// Created, not refused. A machine with no machine_name is an expected
	// state, and refusing to create a box over an ownership setting would be
	// out of proportion.
	if err != nil {
		t.Fatalf("box creation was refused over an ownership setting: %v", err)
	}
	bm, err := models.LoadBoxMeta(cfg, "local", indexName)
	if err != nil {
		t.Fatal(err)
	}
	if bm.WriteOwner != "" {
		t.Errorf("write_owner = %q, want unowned", bm.WriteOwner)
	}
}

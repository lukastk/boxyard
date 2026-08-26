package cmds

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
)

func strs(xs ...string) *[]string { return &xs }

func TestModifyBoxMetaSetsGroups(t *testing.T) {
	cfg := newTestYard(t)
	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "grouped", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}

	got, err := ModifyBoxMeta(cfg, indexName, BoxMetaModifications{
		Groups: strs("backend", "python"),
	})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(got.Groups, ",") != "backend,python" {
		t.Fatalf("groups = %v", got.Groups)
	}

	// It must be on DISK, not just in the returned value.
	meta, err := models.GetBoxyardMeta(cfg, true)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(meta.ByIndexName()[indexName].Groups, ",") != "backend,python" {
		t.Fatal("the change did not reach boxmeta.toml")
	}
}

// The object written back must be re-read from boxmeta.toml, not taken from the
// registry cache: the cache is a snapshot of the last refresh, and anything
// that reached the file since — a META pull from another machine among them —
// would be silently overwritten with the older values.
func TestModifyBoxMetaReReadsFromDisk(t *testing.T) {
	cfg := newTestYard(t)
	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "raced", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	meta, err := models.GetBoxyardMeta(cfg, true)
	if err != nil {
		t.Fatal(err)
	}
	bm := meta.ByIndexName()[indexName]

	// Simulate a META pull landing after the cache was written: change the file
	// behind the cache's back.
	onDisk, err := models.LoadBoxMeta(cfg, bm.StorageLocation, indexName)
	if err != nil {
		t.Fatal(err)
	}
	onDisk.CreatorHostname = "another-machine"
	if err := onDisk.Save(cfg); err != nil {
		t.Fatal(err)
	}

	// A modification that says nothing about creator_hostname must not undo it.
	got, err := ModifyBoxMeta(cfg, indexName, BoxMetaModifications{Groups: strs("g")})
	if err != nil {
		t.Fatal(err)
	}
	if got.CreatorHostname != "another-machine" {
		t.Fatalf("creator_hostname = %q; the stale cache overwrote the file", got.CreatorHostname)
	}
}

func TestModifyBoxMetaRejectsAVirtualGroup(t *testing.T) {
	cfg := newTestYard(t)
	cfg.VirtualBoxGroups["everything"] = &config.VirtualBoxGroupConfig{
		SymlinkName: "everything", FilterExpr: "NOT null",
	}
	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "virtual", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}

	// A virtual group is a FILTER over the yard; membership is computed, so
	// "adding" a box to one would be silently undone on the next refresh.
	_, err = ModifyBoxMeta(cfg, indexName, BoxMetaModifications{Groups: strs("everything")})
	if err == nil || !strings.Contains(err.Error(), "virtual box group") {
		t.Fatalf("want a refusal naming the virtual group, got %v", err)
	}
}

func TestModifyBoxMetaEnforcesUniqueNames(t *testing.T) {
	cfg := newTestYard(t)
	cfg.BoxGroups["solo"] = &config.BoxGroupConfig{SymlinkName: "solo", UniqueBoxNames: true}

	first, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "twin", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ModifyBoxMeta(cfg, first, BoxMetaModifications{Groups: strs("solo")}); err != nil {
		t.Fatal(err)
	}
	second, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "twin", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}

	_, err = ModifyBoxMeta(cfg, second, BoxMetaModifications{Groups: strs("solo")})
	var conflict *NameConflictError
	if !errors.As(err, &conflict) {
		t.Fatalf("want a NameConflictError, got %v", err)
	}
	// The message must name the clashing name — a refusal you cannot act on is
	// a refusal people work around.
	if !strings.Contains(err.Error(), "'twin'") {
		t.Errorf("the refusal does not name the box: %s", err)
	}
}

func TestModifyBoxMetaRejectsAParentCycle(t *testing.T) {
	cfg := newTestYard(t)
	parent, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "parent", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	child, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "child", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	meta, err := models.GetBoxyardMeta(cfg, true)
	if err != nil {
		t.Fatal(err)
	}
	parentID := meta.ByIndexName()[parent].BoxID()
	childID := meta.ByIndexName()[child].BoxID()

	if _, err := ModifyBoxMeta(cfg, child, BoxMetaModifications{Parents: strs(parentID)}); err != nil {
		t.Fatal(err)
	}
	// Now make the parent a child of its own child.
	_, err = ModifyBoxMeta(cfg, parent, BoxMetaModifications{Parents: strs(childID)})
	if err == nil || !strings.Contains(err.Error(), "cycle") {
		t.Fatalf("want a cycle refusal, got %v", err)
	}
}

func TestModifyBoxMetaEnforcesSingleParent(t *testing.T) {
	cfg := newTestYard(t)
	cfg.SingleParent = true
	child, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "child", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	_, err = ModifyBoxMeta(cfg, child, BoxMetaModifications{
		Parents: strs("20240102_aaaaa", "20240103_bbbbb"),
	})
	if err == nil || !strings.Contains(err.Error(), "single_parent") {
		t.Fatalf("want a single-parent refusal, got %v", err)
	}
}

// A parent that has not been synced here yet is a WARNING, not a refusal:
// boxmetas arrive by sync, so naming one this machine has not pulled is normal,
// and blocking it would make the order of operations matter.
func TestModifyBoxMetaWarnsAboutADanglingParent(t *testing.T) {
	cfg := newTestYard(t)
	child, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "child", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ModifyBoxMeta(cfg, child, BoxMetaModifications{
		Parents: strs("20240102_nothere"),
	}); err != nil {
		t.Fatalf("a not-yet-synced parent must be accepted: %v", err)
	}
	meta, err := models.GetBoxyardMeta(cfg, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(meta.ByIndexName()[child].Parents) != 1 {
		t.Fatal("the parent was not recorded")
	}
}

func TestModifyBoxMetaUnknownBox(t *testing.T) {
	cfg := newTestYard(t)
	if _, err := ModifyBoxMeta(cfg, "20240102_aaaaa__nope", BoxMetaModifications{}); err == nil {
		t.Fatal("expected an error")
	}
}

// A box carrying a key a NEWER boxyard wrote must not be rewritten by this
// build: Save refuses, because the alternative is silently discarding it.
func TestModifyBoxMetaRefusesToStripANewerKey(t *testing.T) {
	cfg := newTestYard(t)
	indexName, err := NewBox(context.Background(), cfg, nil, NewBoxOptions{BoxName: "from-the-future", InitialiseGit: false})
	if err != nil {
		t.Fatal(err)
	}
	meta, err := models.GetBoxyardMeta(cfg, true)
	if err != nil {
		t.Fatal(err)
	}
	metaPath, err := meta.ByIndexName()[indexName].LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		t.Fatal(err)
	}
	body, err := os.ReadFile(metaPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(metaPath, append(body, []byte("\nfuture_key = \"x\"\n")...), 0o644); err != nil {
		t.Fatal(err)
	}

	_, err = ModifyBoxMeta(cfg, indexName, BoxMetaModifications{Groups: strs("g")})
	if err == nil || !strings.Contains(err.Error(), "future_key") {
		t.Fatalf("want a refusal naming the unknown key, got %v", err)
	}
	// And the file must be untouched.
	after, err := os.ReadFile(metaPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(after), "future_key") {
		t.Fatal("the newer key was stripped from boxmeta.toml")
	}
	_ = filepath.Dir(metaPath)
}

package syncpolicy

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
)

const hour = 3600
const now = 1_800_000_000.0

func testConfig(t *testing.T, policies map[string]*config.SyncPolicyConfig) *config.Config {
	t.Helper()
	root := t.TempDir()
	return &config.Config{
		DefaultStorageLocation: "remote",
		BoxyardDataPath:        filepath.Join(root, "boxyard"),
		UserBoxesPath:          filepath.Join(root, "boxes"),
		UserBoxGroupsPath:      filepath.Join(root, "groups"),
		StorageLocations: map[string]*config.StorageConfig{
			"remote": {StorageType: "local", StorePath: filepath.Join(root, "store")},
		},
		SyncPolicies: policies,
	}
}

func testBox(name string, groups ...string) *models.BoxMeta {
	if groups == nil {
		groups = []string{}
	}
	return &models.BoxMeta{
		CreationTimestampUTC: "20260822", BoxSubid: "aaaaa", Name: name,
		StorageLocation: "remote", CreatorHostname: "host",
		Groups: groups, Parents: []string{},
	}
}

// The fleet's real shape: cold is archived+dormant, and NOT null.
func fleetPolicies() map[string]*config.SyncPolicyConfig {
	return map[string]*config.SyncPolicyConfig{
		"default": {DataInterval: "6h", MetaInterval: "15m"},
		"cold":    {DataInterval: "7d", Groups: []string{"archived", "dormant"}},
	}
}

func markChecked(t *testing.T, cfg *config.Config, bm *models.BoxMeta, part enums.BoxPart, when float64) {
	t.Helper()
	if err := WriteCheckRecord(cfg, bm.IndexName(), part, when, nil, nil); err != nil {
		t.Fatal(err)
	}
}

// TestNoPolicyConfigMeansAlwaysDue pins the property that makes this shippable
// to a fleet where four of five machines have not opted in.
func TestNoPolicyConfigMeansAlwaysDue(t *testing.T) {
	cfg := testConfig(t, nil)
	boxes := []*models.BoxMeta{testBox("a"), testBox("b"), testBox("c")}
	result, err := DueBoxes(cfg, boxes, enums.PartData, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Due) != 3 || len(result.Skipped) != 0 {
		t.Fatalf("due=%v skipped=%v; every box must be due with no policies", result.Due, result.Skipped)
	}
}

func TestCheckedRecentlyIsSkipped(t *testing.T) {
	cfg := testConfig(t, fleetPolicies())
	box := testBox("a", "proj")
	markChecked(t, cfg, box, enums.PartData, now-hour)
	result, err := DueBoxes(cfg, []*models.BoxMeta{box}, enums.PartData, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Due) != 0 || len(result.Skipped) != 1 {
		t.Fatalf("due=%v skipped=%v", result.Due, result.Skipped)
	}
}

// TestTheBoundaryIsInclusive: `age >= interval`, not `>`. At a 6h cadence on a
// 15-minute tick, an exclusive boundary pushes every box to the NEXT tick,
// quietly turning a 6h cadence into 6h15m for every box in the yard.
func TestTheBoundaryIsInclusive(t *testing.T) {
	for _, tc := range []struct {
		age     float64
		wantDue bool
	}{
		{6*hour - 1, false},
		{6 * hour, true},
		{6*hour + 1, true},
	} {
		cfg := testConfig(t, fleetPolicies())
		box := testBox("a", "proj")
		markChecked(t, cfg, box, enums.PartData, now-tc.age)
		result, err := DueBoxes(cfg, []*models.BoxMeta{box}, enums.PartData, now)
		if err != nil {
			t.Fatal(err)
		}
		if got := len(result.Due) == 1; got != tc.wantDue {
			t.Errorf("age %.0fs: due=%v, want %v", tc.age, got, tc.wantDue)
		}
	}
}

func TestMetaAndDataAreScheduledIndependently(t *testing.T) {
	cfg := testConfig(t, fleetPolicies()) // meta 15m, data 6h
	box := testBox("a", "proj")
	markChecked(t, cfg, box, enums.PartData, now-30*60)
	markChecked(t, cfg, box, enums.PartMeta, now-30*60)

	meta, _ := DueBoxes(cfg, []*models.BoxMeta{box}, enums.PartMeta, now)
	data, _ := DueBoxes(cfg, []*models.BoxMeta{box}, enums.PartData, now)
	if len(meta.Due) != 1 {
		t.Error("META should be due after 30 min at a 15m cadence")
	}
	if len(data.Due) != 0 {
		t.Error("DATA should not be due after 30 min at a 6h cadence")
	}
}

func TestMostOverdueFirst(t *testing.T) {
	cfg := testConfig(t, fleetPolicies())
	a, b, c := testBox("a", "proj"), testBox("b", "proj"), testBox("c", "proj")
	markChecked(t, cfg, a, enums.PartData, now-7*hour)
	markChecked(t, cfg, b, enums.PartData, now-40*hour)
	markChecked(t, cfg, c, enums.PartData, now-12*hour)
	result, _ := DueBoxes(cfg, []*models.BoxMeta{a, b, c}, enums.PartData, now)
	want := []string{b.IndexName(), c.IndexName(), a.IndexName()}
	for i := range want {
		if result.Due[i] != want[i] {
			t.Fatalf("order = %v, want %v", result.Due, want)
		}
	}
}

func TestAConflictedBoxIsReportedAndStillDue(t *testing.T) {
	cfg := testConfig(t, map[string]*config.SyncPolicyConfig{
		"default": {DataInterval: "6h"},
		"cold":    {DataInterval: "7d", Groups: []string{"archived"}},
		"hot":     {DataInterval: "1h", Groups: []string{"live"}},
	})
	box := testBox("a", "archived", "live")
	result, err := DueBoxes(cfg, []*models.BoxMeta{box}, enums.PartData, now)
	if err != nil {
		t.Fatalf("a conflict must not fail the pass: %v", err)
	}
	if len(result.Due) != 1 {
		t.Error("a conflicted box must still sync — the ambiguity is about how often")
	}
	if len(result.Conflicts) != 1 || result.Conflicts[0].Dimension != "data_interval" {
		t.Errorf("conflicts = %v", result.Conflicts)
	}
}

func TestANonSchedulablePartIsRefused(t *testing.T) {
	cfg := testConfig(t, fleetPolicies())
	if _, err := DueBoxes(cfg, []*models.BoxMeta{testBox("a")}, enums.PartConf, now); err == nil {
		t.Error("CONF has no cadence and must be refused")
	}
}

// TestAnUnusableCheckRecordMeansDue pins the one-directional degradation: a
// record that is missing, truncated, foreign or undecodable all mean "due now",
// never "up to date".
func TestAnUnusableCheckRecordMeansDue(t *testing.T) {
	for _, content := range []string{
		"", "{", "null", "[1,2,3]", `{"last_checked_unix": "soon"}`, `{"checked": 1}`,
		"\x00\xff not utf-8",
	} {
		cfg := testConfig(t, fleetPolicies())
		box := testBox("a", "proj")
		path := CheckRecordPath(cfg, box.IndexName(), enums.PartData)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		if ReadCheckRecord(cfg, box.IndexName(), enums.PartData) != nil {
			t.Errorf("%q parsed as a usable record", content)
		}
		result, _ := DueBoxes(cfg, []*models.BoxMeta{box}, enums.PartData, now)
		if len(result.Due) != 1 {
			t.Errorf("%q: box was not due", content)
		}
	}
}

func TestWriteLeavesNoTempFilesBehind(t *testing.T) {
	cfg := testConfig(t, fleetPolicies())
	box := testBox("a")
	for i := 0; i < 3; i++ {
		markChecked(t, cfg, box, enums.PartData, now+float64(i))
	}
	dir := filepath.Dir(CheckRecordPath(cfg, box.IndexName(), enums.PartData))
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "data.json" {
		names := []string{}
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Fatalf("leftovers in %s: %v", dir, names)
	}
}

// TestAnUnstampedWriteKeepsTheStamp: an ordinary pass records only a timestamp;
// wiping the stamp would disarm the skip filter every time an unfiltered pass
// ran, which is every machine, every day.
func TestAnUnstampedWriteKeepsTheStamp(t *testing.T) {
	cfg := testConfig(t, fleetPolicies())
	box := testBox("a")
	modtime, size := "2026-08-27T21:44:32Z", int64(139)
	if err := WriteCheckRecord(cfg, box.IndexName(), enums.PartMeta, now, &modtime, &size); err != nil {
		t.Fatal(err)
	}
	if err := WriteCheckRecord(cfg, box.IndexName(), enums.PartMeta, now+60, nil, nil); err != nil {
		t.Fatal(err)
	}
	record := ReadCheckRecord(cfg, box.IndexName(), enums.PartMeta)
	if record == nil || record.RemoteModTime == nil || *record.RemoteModTime != modtime {
		t.Fatalf("the stamp was erased by an unstamped write: %+v", record)
	}
	if record.LastCheckedUnix != now+60 {
		t.Errorf("timestamp not updated: %v", record.LastCheckedUnix)
	}
}

func TestRemoteLooksUnchangedNeedsBothFields(t *testing.T) {
	modtime, size := "T1", int64(10)
	record := &CheckRecord{LastCheckedUnix: now, RemoteModTime: &modtime, RemoteSize: &size}
	other, otherSize := "T2", int64(11)

	if !RemoteLooksUnchanged(record, &modtime, &size) {
		t.Error("identical stamps should compare equal")
	}
	if RemoteLooksUnchanged(record, &other, &size) {
		t.Error("a different ModTime must read as changed")
	}
	// The case that broke the design note's first safety argument: rclone DOES
	// preserve ModTime across a push, so Size is compared too.
	if RemoteLooksUnchanged(record, &modtime, &otherSize) {
		t.Error("a preserved ModTime with a different Size must read as changed")
	}
	if RemoteLooksUnchanged(nil, &modtime, &size) {
		t.Error("no record must read as changed")
	}
	if RemoteLooksUnchanged(&CheckRecord{LastCheckedUnix: now}, &modtime, &size) {
		t.Error("a record with no stamp must read as changed")
	}
	if RemoteLooksUnchanged(record, nil, &size) || RemoteLooksUnchanged(record, &modtime, nil) {
		t.Error("an unlisted remote must read as changed")
	}
}

// settle makes a box look fully reconciled: boxmeta on disk, a matching merge
// base, and a check record carrying a remote stamp. Without all three, a filter
// test cannot tell "correctly needed" from "needed for an unrelated reason" —
// which is how the first version of the test below passed against a broken
// filter.
func settle(t *testing.T, cfg *config.Config, box *models.BoxMeta) RemoteStamp {
	t.Helper()
	metaPath, err := box.LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(metaPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := box.Save(cfg); err != nil {
		t.Fatal(err)
	}
	if err := models.RecordMetaBase(cfg, box); err != nil {
		t.Fatal(err)
	}
	modtime, size := "2026-08-27T21:44:32Z", int64(139)
	if err := WriteCheckRecord(cfg, box.IndexName(), enums.PartMeta, now, &modtime, &size); err != nil {
		t.Fatal(err)
	}
	return RemoteStamp{ModTime: &modtime, Size: &size}
}

func TestAFullySettledBoxIsSkippable(t *testing.T) {
	cfg := testConfig(t, fleetPolicies())
	box := testBox("quiet")
	stamp := settle(t, cfg, box)
	needed, skippable := MetaBoxesNeedingSync(cfg, []*models.BoxMeta{box},
		map[string]RemoteStamp{box.IndexName(): stamp})
	if len(skippable) != 1 || len(needed) != 0 {
		t.Fatalf("needed=%v skippable=%v; a settled box must be skippable, or "+
			"every other filter test passes vacuously", needed, skippable)
	}
}

func TestABoxMissingFromTheListingIsNeverSkipped(t *testing.T) {
	cfg := testConfig(t, fleetPolicies())
	box := testBox("ghost")
	// Settled in every other respect, so the ONLY reason to sync it is that the
	// listing does not mention it.
	settle(t, cfg, box)
	needed, skippable := MetaBoxesNeedingSync(cfg, []*models.BoxMeta{box}, map[string]RemoteStamp{})
	if len(needed) != 1 || len(skippable) != 0 {
		t.Fatalf("needed=%v skippable=%v; an unlisted box must always be checked", needed, skippable)
	}
}

func TestALocalEditIsAlwaysSynced(t *testing.T) {
	cfg := testConfig(t, fleetPolicies())
	box := testBox("edited")
	stamp := settle(t, cfg, box)

	// Edit the boxmeta on disk, leaving the base and the remote stamp alone.
	// This is the half a remote-only filter silently drops: fast inbox, dead
	// outbox.
	box.Groups = append(box.Groups, "a-new-group")
	if err := box.Save(cfg); err != nil {
		t.Fatal(err)
	}
	needed, skippable := MetaBoxesNeedingSync(cfg, []*models.BoxMeta{box},
		map[string]RemoteStamp{box.IndexName(): stamp})
	if len(needed) != 1 || len(skippable) != 0 {
		t.Fatalf("needed=%v skippable=%v; a local edit must always be pushed", needed, skippable)
	}
}

func TestABoxWithNoMergeBaseIsNeverSkipped(t *testing.T) {
	// No base means "cannot tell whether there is a local edit", which must
	// cost a sync rather than skip one. This is what makes the upgrade safe:
	// every box is checked once, then settles.
	cfg := testConfig(t, fleetPolicies())
	box := testBox("baseless")
	stamp := settle(t, cfg, box)

	if err := os.Remove(box.LocalMetaBasePath(cfg)); err != nil {
		t.Fatal(err)
	}
	needed, skippable := MetaBoxesNeedingSync(cfg, []*models.BoxMeta{box},
		map[string]RemoteStamp{box.IndexName(): stamp})
	if len(needed) != 1 || len(skippable) != 0 {
		t.Fatalf("needed=%v skippable=%v; a box with no base must be checked",
			needed, skippable)
	}
}

func TestABoxWhoseLocalMetaIsGoneIsNeverSkipped(t *testing.T) {
	// A boxmeta that has vanished locally is not "nothing to push" — it is a
	// state the real sync path must look at and report on. Skipping it here
	// would hide the box's disappearance behind a filter.
	cfg := testConfig(t, fleetPolicies())
	box := testBox("vanished")
	stamp := settle(t, cfg, box)

	metaPath, err := box.LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(metaPath); err != nil {
		t.Fatal(err)
	}
	needed, skippable := MetaBoxesNeedingSync(cfg, []*models.BoxMeta{box},
		map[string]RemoteStamp{box.IndexName(): stamp})
	if len(needed) != 1 || len(skippable) != 0 {
		t.Fatalf("needed=%v skippable=%v; a missing local boxmeta must be checked",
			needed, skippable)
	}
}

func TestABoxWhoseLocalMetaIsUnreadableIsNeverSkipped(t *testing.T) {
	// Corruption must reach the real sync path, which reports it properly,
	// rather than being silently skipped by an optimisation.
	cfg := testConfig(t, fleetPolicies())
	box := testBox("corrupt")
	stamp := settle(t, cfg, box)

	metaPath, err := box.LocalPartPath(cfg, enums.PartMeta)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(metaPath, []byte("groups = [unclosed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	needed, skippable := MetaBoxesNeedingSync(cfg, []*models.BoxMeta{box},
		map[string]RemoteStamp{box.IndexName(): stamp})
	if len(needed) != 1 || len(skippable) != 0 {
		t.Fatalf("needed=%v skippable=%v; an unreadable boxmeta must be checked",
			needed, skippable)
	}
}

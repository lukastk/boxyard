package syncengine

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/models"
	"github.com/oklog/ulid/v2"
)

// --- fake prober -------------------------------------------------------

type fakeProber struct {
	localExists, localIsDir, localIsEmpty bool
	remoteExists, remoteIsDir             bool
	local, remote                         *models.SyncRecord
	mtime                                 time.Time
	mtimeFound                            bool
	err                                   error
}

func (f *fakeProber) PathExists(_ context.Context, remote, _ string) (bool, bool, error) {
	if f.err != nil {
		return false, false, f.err
	}
	if remote == "" {
		return f.localExists, f.localIsDir, nil
	}
	return f.remoteExists, f.remoteIsDir, nil
}

func (f *fakeProber) ReadSyncRecord(_ context.Context, remote, _ string) (*models.SyncRecord, error) {
	if remote == "" {
		return f.local, nil
	}
	return f.remote, nil
}

func (f *fakeProber) LocalIsEmptyDir(string) (bool, error) { return f.localIsEmpty, nil }

func (f *fakeProber) LocalLastModified(string, map[string]bool) (time.Time, bool, error) {
	return f.mtime, f.mtimeFound, nil
}

func req() StatusRequest {
	return StatusRequest{
		LocalPath: "/local", LocalSyncRecordPath: "/lrec",
		Remote: "rem", RemotePath: "rpath", RemoteSyncRecordPath: "/rrec",
	}
}

func rec(id string, complete bool) *models.SyncRecord {
	u := ulid.MustParse(id)
	return &models.SyncRecord{
		ULID:           id,
		Timestamp:      ulid.Time(u.Time()).UTC().Format("2006-01-02T15:04:05.000000Z"),
		SyncComplete:   complete,
		SyncerHostname: "h",
	}
}

// --- the exhaustive differential ---------------------------------------

type goldenFile struct {
	UlidA     string `json:"ulid_a"`
	UlidB     string `json:"ulid_b"`
	Scenarios []struct {
		LocalExists  bool    `json:"local_exists"`
		LocalIsDir   bool    `json:"local_is_dir"`
		LocalIsEmpty bool    `json:"local_is_empty"`
		RemoteExists bool    `json:"remote_exists"`
		RemoteIsDir  bool    `json:"remote_is_dir"`
		LocalRec     []any   `json:"local_rec"`
		RemoteRec    []any   `json:"remote_rec"`
		Mtime        *string `json:"mtime"`
	} `json:"scenarios"`
	PythonVerdicts []string `json:"python_verdicts"`
}

// TestMatchesPythonAcrossEntireInputSpace replays every combination of the
// inputs the state machine reads and asserts Go reaches the same verdict as the
// Python implementation.
//
// This is the most important test in the codebase. The state machine decides
// whether to push, pull, or refuse; a wrong answer overwrites or loses a box.
// Hand-written cases would cover the branches someone thought of — this covers
// the whole space, including the combinations nobody would think to write down.
func TestMatchesPythonAcrossEntireInputSpace(t *testing.T) {
	raw, err := os.ReadFile("testdata/sync_status_scenarios.json")
	if err != nil {
		t.Fatalf("golden scenarios missing: %v", err)
	}
	var g goldenFile
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	if len(g.Scenarios) == 0 || len(g.Scenarios) != len(g.PythonVerdicts) {
		t.Fatalf("golden is malformed: %d scenarios, %d verdicts", len(g.Scenarios), len(g.PythonVerdicts))
	}

	mkrec := func(spec []any) *models.SyncRecord {
		if spec == nil {
			return nil
		}
		id := g.UlidA
		if spec[0].(string) == "B" {
			id = g.UlidB
		}
		return rec(id, spec[1].(bool))
	}

	var mismatches int
	for i, s := range g.Scenarios {
		local, remote := mkrec(s.LocalRec), mkrec(s.RemoteRec)

		// Anchor the mtime to the local record's time, or to ULID A when there
		// is no local record — the same rule the golden was generated with.
		anchor := ulid.Time(ulid.MustParse(g.UlidA).Time()).UTC()
		if local != nil {
			anchor, _ = local.Time()
		}
		var mtime time.Time
		found := false
		if s.Mtime != nil {
			found = true
			if *s.Mtime == "before" {
				mtime = anchor.Add(-10 * time.Second)
			} else {
				mtime = anchor.Add(10 * time.Second)
			}
		}

		p := &fakeProber{
			localExists: s.LocalExists, localIsDir: s.LocalIsDir, localIsEmpty: s.LocalIsEmpty,
			remoteExists: s.RemoteExists, remoteIsDir: s.RemoteIsDir,
			local: local, remote: remote, mtime: mtime, mtimeFound: found,
		}
		got := "RAISE"
		if st, err := GetSyncStatus(context.Background(), p, req()); err == nil {
			got = string(st.Condition)
		}
		if want := g.PythonVerdicts[i]; got != want {
			mismatches++
			if mismatches <= 5 {
				t.Errorf("scenario %d: go=%s python=%s\n  %+v", i, got, want, s)
			}
		}
	}
	if mismatches > 0 {
		t.Fatalf("%d/%d scenarios disagree with Python", mismatches, len(g.Scenarios))
	}
	t.Logf("all %d scenarios agree with the Python implementation", len(g.Scenarios))
}

// --- readable cases for the branches that matter most -------------------

func TestSyncedWhenRecordsMatchAndNothingChanged(t *testing.T) {
	r := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	rt, _ := r.Time()
	p := &fakeProber{
		localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
		local: r, remote: r, mtime: rt.Add(-time.Hour), mtimeFound: true,
	}
	st, err := GetSyncStatus(context.Background(), p, req())
	if err != nil || st.Condition != Synced {
		t.Fatalf("got %v (%v)", st.Condition, err)
	}
}

func TestNeedsPushWhenLocalChangedSinceMatchingRecord(t *testing.T) {
	r := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	rt, _ := r.Time()
	p := &fakeProber{
		localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
		local: r, remote: r, mtime: rt.Add(time.Hour), mtimeFound: true,
	}
	st, _ := GetSyncStatus(context.Background(), p, req())
	if st.Condition != NeedsPush {
		t.Fatalf("got %v", st.Condition)
	}
}

// The remote moved on and we also have local changes — the case that must
// never be resolved automatically.
func TestConflictWhenBothSidesMoved(t *testing.T) {
	older := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	newer := rec("01KWQBZ23B3W2D8F9JV0BTTXA4", true)
	ot, _ := older.Time()
	p := &fakeProber{
		localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
		local: older, remote: newer, mtime: ot.Add(time.Hour), mtimeFound: true,
	}
	st, _ := GetSyncStatus(context.Background(), p, req())
	if st.Condition != Conflict {
		t.Fatalf("got %v, want conflict", st.Condition)
	}
}

func TestNeedsPullWhenRemoteMovedAndLocalDidNot(t *testing.T) {
	older := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	newer := rec("01KWQBZ23B3W2D8F9JV0BTTXA4", true)
	ot, _ := older.Time()
	p := &fakeProber{
		localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
		local: older, remote: newer, mtime: ot.Add(-time.Hour), mtimeFound: true,
	}
	st, _ := GetSyncStatus(context.Background(), p, req())
	if st.Condition != NeedsPull {
		t.Fatalf("got %v, want needs_pull", st.Condition)
	}
}

// Matching INCOMPLETE records on both sides mean this machine's push was
// interrupted — push writes the incomplete record to both sides.
func TestInterruptedPushFromThisMachine(t *testing.T) {
	r := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", false)
	p := &fakeProber{
		localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
		local: r, remote: r, mtimeFound: true,
	}
	st, _ := GetSyncStatus(context.Background(), p, req())
	if st.Condition != SyncToRemoteIncomplete {
		t.Fatalf("got %v", st.Condition)
	}
}

func TestInterruptedPullIsLocalOnly(t *testing.T) {
	p := &fakeProber{
		localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
		local:  rec("01KRRZXHQ1T13ADRQWFT1E4ESH", false),
		remote: rec("01KWQBZ23B3W2D8F9JV0BTTXA4", true), mtimeFound: true,
	}
	st, _ := GetSyncStatus(context.Background(), p, req())
	if st.Condition != SyncFromRemoteIncomplete {
		t.Fatalf("got %v", st.Condition)
	}
}

func TestIncompleteRecordsWithDifferentULIDsIsAnError(t *testing.T) {
	p := &fakeProber{
		localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
		local:  rec("01KRRZXHQ1T13ADRQWFT1E4ESH", false),
		remote: rec("01KWQBZ23B3W2D8F9JV0BTTXA4", false), mtimeFound: true,
	}
	st, _ := GetSyncStatus(context.Background(), p, req())
	if st.Condition != Error {
		t.Fatalf("got %v", st.Condition)
	}
	if st.ErrorMessage == "" {
		t.Error("Error condition carried no message")
	}
}

func TestExcludedWhenOnlyRemoteExists(t *testing.T) {
	p := &fakeProber{remoteExists: true, remoteIsDir: true, remote: rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)}
	st, _ := GetSyncStatus(context.Background(), p, req())
	if st.Condition != Excluded {
		t.Fatalf("got %v", st.Condition)
	}
}

// Neither side exists — common for `conf`, which most boxes never have.
func TestNeitherSideExistsIsSynced(t *testing.T) {
	p := &fakeProber{}
	st, _ := GetSyncStatus(context.Background(), p, req())
	if st.Condition != Synced {
		t.Fatalf("got %v", st.Condition)
	}
}

func TestFileVersusDirectoryMismatchIsFatal(t *testing.T) {
	p := &fakeProber{
		localExists: true, localIsDir: true,
		remoteExists: true, remoteIsDir: false,
		remote: rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true),
	}
	if _, err := GetSyncStatus(context.Background(), p, req()); err == nil {
		t.Fatal("a file/directory mismatch was not treated as fatal")
	}
}

func TestRemoteContentWithoutRecordIsAnError(t *testing.T) {
	p := &fakeProber{remoteExists: true, remoteIsDir: true, remote: nil}
	st, _ := GetSyncStatus(context.Background(), p, req())
	if st.Condition != Error {
		t.Fatalf("got %v", st.Condition)
	}
}

func TestProbeErrorsPropagate(t *testing.T) {
	p := &fakeProber{err: os.ErrPermission}
	if _, err := GetSyncStatus(context.Background(), p, req()); err == nil {
		t.Fatal("a probe failure was swallowed")
	}
}

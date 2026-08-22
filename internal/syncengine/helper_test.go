package syncengine

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/oklog/ulid/v2"
)

// --- fakes -------------------------------------------------------------

type event struct {
	kind   string // "record", "mkdir", "sync", "purge"
	remote string
	path   string
	rec    *models.SyncRecord
}

type fakeStorage struct {
	fakeProber
	events   []event
	syncOK   bool
	syncErr  error
	syncOpts []SyncOptions
	// records written, so a test can read back what a pull would find
	remoteRecOverride *models.SyncRecord
	// remoteVanishesAfter makes ReadSyncRecord return nil for the remote from
	// the Nth call onward, simulating a record disappearing mid-sync.
	remoteVanishesAfter int
	remoteReads         int
}

func (f *fakeStorage) ReadSyncRecord(ctx context.Context, remote, p string) (*models.SyncRecord, error) {
	if remote != "" {
		f.remoteReads++
		if f.remoteVanishesAfter > 0 && f.remoteReads > f.remoteVanishesAfter {
			return nil, nil
		}
		if f.remoteRecOverride != nil {
			return f.remoteRecOverride, nil
		}
	}
	return f.fakeProber.ReadSyncRecord(ctx, remote, p)
}

func (f *fakeStorage) Mkdir(_ context.Context, remote, p string) error {
	f.events = append(f.events, event{kind: "mkdir", remote: remote, path: p})
	return nil
}

func (f *fakeStorage) Sync(_ context.Context, opts SyncOptions) (bool, string, string, error) {
	f.events = append(f.events, event{kind: "sync", remote: opts.Dest, path: opts.DestPath})
	f.syncOpts = append(f.syncOpts, opts)
	return f.syncOK, "out", "err", f.syncErr
}

func (f *fakeStorage) Purge(_ context.Context, remote, p string) error {
	f.events = append(f.events, event{kind: "purge", remote: remote, path: p})
	return nil
}

func (f *fakeStorage) WriteSyncRecord(_ context.Context, remote, p string, rec models.SyncRecord) error {
	cp := rec
	f.events = append(f.events, event{kind: "record", remote: remote, path: p, rec: &cp})
	return nil
}

func (f *fakeStorage) kinds() []string {
	var out []string
	for _, e := range f.events {
		out = append(out, e.kind)
	}
	return out
}

type fakePerms struct{ generated, applied []string }

func (p *fakePerms) Generate(root string) (bool, error) {
	p.generated = append(p.generated, root)
	return true, nil
}
func (p *fakePerms) Apply(root string) ([]string, error) {
	p.applied = append(p.applied, root)
	return nil, nil
}

func helperReq() HelperRequest {
	return HelperRequest{
		StatusRequest:         req(),
		Setting:               enums.SyncCareful,
		LocalSyncBackupsPath:  "/backups",
		RemoteSyncBackupsPath: "sync_backups",
		DeleteBackup:          true,
		SyncerHostname:        "testhost",
	}
}

func dir(d enums.SyncDirection) *enums.SyncDirection { return &d }

// --- guards ------------------------------------------------------------

func TestEmptyRemotePathIsRefused(t *testing.T) {
	r := helperReq()
	r.RemotePath = ""
	_, _, err := Run(context.Background(), &fakeStorage{}, &fakePerms{}, r)
	var want *InvalidRemotePathError
	if !errors.As(err, &want) {
		t.Fatalf("expected InvalidRemotePathError, got %v", err)
	}
}

func TestAutoDirectionRequiresCareful(t *testing.T) {
	r := helperReq()
	r.Setting = enums.SyncForce
	r.Direction = nil
	if _, _, err := Run(context.Background(), &fakeStorage{}, &fakePerms{}, r); err == nil {
		t.Fatal("auto direction was allowed outside CAREFUL")
	}
}

func TestSyncedDoesNothing(t *testing.T) {
	rc := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	rt, _ := rc.Time()
	s := &fakeStorage{fakeProber: fakeProber{
		localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
		local: rc, remote: rc, mtime: rt.Add(-time.Hour), mtimeFound: true,
	}}
	_, synced, err := Run(context.Background(), s, &fakePerms{}, helperReq())
	if err != nil || synced {
		t.Fatalf("synced=%v err=%v", synced, err)
	}
	if len(s.events) != 0 {
		t.Errorf("a synced box triggered %v", s.kinds())
	}
}

// --- ordering: the safety mechanism ------------------------------------

func pushScenario(syncOK bool) *fakeStorage {
	rc := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	rt, _ := rc.Time()
	return &fakeStorage{
		fakeProber: fakeProber{
			localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
			local: rc, remote: rc, mtime: rt.Add(time.Hour), mtimeFound: true,
		},
		syncOK: syncOK,
	}
}

// A push must write an INCOMPLETE record to BOTH sides BEFORE transferring.
// Matching incomplete ULIDs on both sides are the proof that this machine owns
// an interrupted push and may retry it.
func TestPushWritesIncompleteRecordToBothSidesFirst(t *testing.T) {
	s := pushScenario(true)
	_, synced, err := Run(context.Background(), s, &fakePerms{}, helperReq())
	if err != nil || !synced {
		t.Fatalf("synced=%v err=%v", synced, err)
	}

	// records: remote incomplete, local incomplete, [mkdir, sync], local
	// complete, remote complete, purge
	var pre []event
	for _, e := range s.events {
		if e.kind == "sync" {
			break
		}
		if e.kind == "record" {
			pre = append(pre, e)
		}
	}
	if len(pre) != 2 {
		t.Fatalf("expected 2 records written before the transfer, got %d (%v)", len(pre), s.kinds())
	}
	if pre[0].rec.SyncComplete || pre[1].rec.SyncComplete {
		t.Error("pre-transfer records must be INCOMPLETE")
	}
	if pre[0].rec.ULID != pre[1].rec.ULID {
		t.Error("both sides must carry the SAME incomplete ULID — that is what proves this machine owns the sync")
	}
	if pre[0].remote == pre[1].remote {
		t.Errorf("expected one remote and one local record, got both on %q", pre[0].remote)
	}

	// After success, both sides get a completed record, then the backup is purged.
	last := s.events[len(s.events)-1]
	if last.kind != "purge" {
		t.Errorf("backup should be purged last, got %v", s.kinds())
	}
	var completed int
	for _, e := range s.events {
		if e.kind == "record" && e.rec.SyncComplete {
			completed++
		}
	}
	if completed != 2 {
		t.Errorf("expected 2 completed records after success, got %d", completed)
	}
}

// A pull only puts the LOCAL side at risk, so only the local record is marked
// incomplete.
func TestPullWritesIncompleteRecordLocallyOnly(t *testing.T) {
	older := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	newer := rec("01KWQBZ23B3W2D8F9JV0BTTXA4", true)
	ot, _ := older.Time()
	s := &fakeStorage{
		fakeProber: fakeProber{
			localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
			local: older, remote: newer, mtime: ot.Add(-time.Hour), mtimeFound: true,
		},
		syncOK:            true,
		remoteRecOverride: newer,
	}
	_, synced, err := Run(context.Background(), s, &fakePerms{}, helperReq())
	if err != nil || !synced {
		t.Fatalf("synced=%v err=%v", synced, err)
	}
	for _, e := range s.events {
		if e.kind == "sync" {
			break
		}
		if e.kind == "record" && e.remote != "" {
			t.Error("a pull must not write an incomplete record to the REMOTE")
		}
	}
}

// On failure the incomplete records are deliberately left behind — they are
// what tells the next run a sync was interrupted, and by whom.
func TestFailedSyncLeavesIncompleteRecords(t *testing.T) {
	s := pushScenario(false)
	_, synced, err := Run(context.Background(), s, &fakePerms{}, helperReq())
	if synced {
		t.Error("a failed sync reported success")
	}
	var failed *SyncFailedError
	if !errors.As(err, &failed) {
		t.Fatalf("expected SyncFailedError, got %v", err)
	}
	for _, e := range s.events {
		if e.kind == "record" && e.rec.SyncComplete {
			t.Error("a completed record was written despite failure")
		}
		if e.kind == "purge" {
			t.Error("the backup was purged despite failure")
		}
	}
}

// --- the CAREFUL gate --------------------------------------------------

// A remote left incomplete by ANOTHER machine must not be overwritten.
func TestCarefulRefusesAnotherMachinesInterruptedPush(t *testing.T) {
	s := &fakeStorage{fakeProber: fakeProber{
		localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
		local:      rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true),
		remote:     rec("01KWQBZ23B3W2D8F9JV0BTTXA4", false),
		mtimeFound: true,
	}}
	_, _, err := Run(context.Background(), s, &fakePerms{}, helperReq())
	var unsafeErr *SyncUnsafeError
	if !errors.As(err, &unsafeErr) {
		t.Fatalf("expected refusal, got %v", err)
	}
	if !strings.Contains(err.Error(), "another machine") {
		t.Errorf("message should explain whose sync it was: %v", err)
	}
	for _, e := range s.events {
		if e.kind == "sync" {
			t.Fatal("a transfer happened despite the refusal")
		}
	}
}

// This machine's own interrupted push IS retryable — same ULID on both sides.
func TestCarefulAllowsRetryOfOwnInterruptedPush(t *testing.T) {
	same := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", false)
	s := &fakeStorage{
		fakeProber: fakeProber{
			localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
			local: same, remote: same, mtimeFound: true,
		},
		syncOK: true,
	}
	_, synced, err := Run(context.Background(), s, &fakePerms{}, helperReq())
	if err != nil || !synced {
		t.Fatalf("own interrupted push should be retryable: synced=%v err=%v", synced, err)
	}
}

func TestCarefulRefusesPushOnConflict(t *testing.T) {
	older := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	newer := rec("01KWQBZ23B3W2D8F9JV0BTTXA4", true)
	ot, _ := older.Time()
	s := &fakeStorage{fakeProber: fakeProber{
		localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
		local: older, remote: newer, mtime: ot.Add(time.Hour), mtimeFound: true,
	}}
	r := helperReq()
	r.Direction = dir(enums.DirectionPush)
	if _, _, err := Run(context.Background(), s, &fakePerms{}, r); err == nil {
		t.Fatal("CAREFUL allowed a push over a conflict")
	}
}

// FORCE and REPLACE deliberately skip the gate.
func TestForceSkipsTheCarefulGate(t *testing.T) {
	older := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	newer := rec("01KWQBZ23B3W2D8F9JV0BTTXA4", true)
	ot, _ := older.Time()
	for _, setting := range []enums.SyncSetting{enums.SyncForce, enums.SyncReplace} {
		s := &fakeStorage{
			fakeProber: fakeProber{
				localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
				local: older, remote: newer, mtime: ot.Add(time.Hour), mtimeFound: true,
			},
			syncOK: true,
		}
		r := helperReq()
		r.Setting = setting
		r.Direction = dir(enums.DirectionPush)
		if _, synced, err := Run(context.Background(), s, &fakePerms{}, r); err != nil || !synced {
			t.Errorf("%s should have pushed over the conflict: synced=%v err=%v", setting, synced, err)
		}
	}
}

// --- other behaviour ---------------------------------------------------

func TestAllowMissingSourceSkips(t *testing.T) {
	s := &fakeStorage{fakeProber: fakeProber{
		remoteExists: true, remoteIsDir: true,
		remote: rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true),
	}}
	r := helperReq()
	r.Direction = dir(enums.DirectionPush)
	r.Setting = enums.SyncForce
	r.AllowMissingSource = true
	_, synced, err := Run(context.Background(), s, &fakePerms{}, r)
	if err != nil || synced {
		t.Fatalf("a missing source should skip: synced=%v err=%v", synced, err)
	}
}

func TestExecManifestGeneratedOnPushAndAppliedOnPull(t *testing.T) {
	p := &fakePerms{}
	s := pushScenario(true)
	r := helperReq()
	r.PreserveExecPerms = true
	if _, _, err := Run(context.Background(), s, p, r); err != nil {
		t.Fatal(err)
	}
	if len(p.generated) != 1 || len(p.applied) != 0 {
		t.Errorf("push should generate, not apply: generated=%v applied=%v", p.generated, p.applied)
	}

	older := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	newer := rec("01KWQBZ23B3W2D8F9JV0BTTXA4", true)
	ot, _ := older.Time()
	p2 := &fakePerms{}
	s2 := &fakeStorage{
		fakeProber: fakeProber{
			localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
			local: older, remote: newer, mtime: ot.Add(-time.Hour), mtimeFound: true,
		},
		syncOK: true, remoteRecOverride: newer,
	}
	r2 := helperReq()
	r2.PreserveExecPerms = true
	if _, _, err := Run(context.Background(), s2, p2, r2); err != nil {
		t.Fatal(err)
	}
	if len(p2.applied) != 1 || len(p2.generated) != 0 {
		t.Errorf("pull should apply, not generate: generated=%v applied=%v", p2.generated, p2.applied)
	}
}

// The remote sync record disappearing between the status probe and the end of
// a pull makes the Python crash with an AttributeError on None. Go reports it,
// and leaves the local record incomplete so the pull can be retried.
func TestPullWithVanishedRemoteRecordIsReported(t *testing.T) {
	older := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	newer := rec("01KWQBZ23B3W2D8F9JV0BTTXA4", true)
	ot, _ := older.Time()
	s := &fakeStorage{
		fakeProber: fakeProber{
			localExists: true, localIsDir: true, remoteExists: true, remoteIsDir: true,
			local: older, remote: newer, mtime: ot.Add(-time.Hour), mtimeFound: true,
		},
		syncOK: true,
		// Readable during the status probe, gone by the post-pull read.
		remoteVanishesAfter: 1,
	}
	_, synced, err := Run(context.Background(), s, &fakePerms{}, helperReq())
	if err == nil {
		t.Fatal("a vanished remote sync record was not reported")
	}
	if synced {
		t.Error("reported success despite the missing record")
	}
	if !strings.Contains(err.Error(), "disappeared") {
		t.Errorf("error should say what happened, got: %v", err)
	}
	// The local record must still be the INCOMPLETE one, so the pull retries.
	var lastLocal *models.SyncRecord
	for _, e := range s.events {
		if e.kind == "record" && e.remote == "" {
			lastLocal = e.rec
		}
	}
	if lastLocal == nil || lastLocal.SyncComplete {
		t.Error("the local record should be left incomplete so the pull can be retried")
	}
}

func TestBackupPathUsesTheRecordULID(t *testing.T) {
	s := pushScenario(true)
	if _, _, err := Run(context.Background(), s, &fakePerms{}, helperReq()); err != nil {
		t.Fatal(err)
	}
	var backup string
	for _, e := range s.events {
		if e.kind == "mkdir" {
			backup = e.path
		}
	}
	if backup == "" {
		t.Fatal("no backup directory was created")
	}
	leaf := backup[strings.LastIndex(backup, "/")+1:]
	if _, err := ulid.ParseStrict(leaf); err != nil {
		t.Errorf("backup directory %q is not named after a ULID", leaf)
	}
}

// A file (not a directory) cannot be an rclone sync destination, so the parent
// is used instead.
func TestFileDestinationUsesParentPath(t *testing.T) {
	rc := rec("01KRRZXHQ1T13ADRQWFT1E4ESH", true)
	rt, _ := rc.Time()
	s := &fakeStorage{
		fakeProber: fakeProber{
			localExists: true, localIsDir: false, remoteExists: true, remoteIsDir: false,
			local: rc, remote: rc, mtime: rt.Add(time.Hour), mtimeFound: true,
		},
		syncOK: true,
	}
	r := helperReq()
	r.RemotePath = "boxyard/boxes/b/boxmeta.toml"
	if _, _, err := Run(context.Background(), s, &fakePerms{}, r); err != nil {
		t.Fatal(err)
	}
	if len(s.syncOpts) != 1 {
		t.Fatalf("expected one sync, got %d", len(s.syncOpts))
	}
	if got := s.syncOpts[0].DestPath; got != "boxyard/boxes/b" {
		t.Errorf("file destination should use the parent, got %q", got)
	}
}

func TestParentPath(t *testing.T) {
	if got := parentPath("a/b/c", false); got != "a/b" {
		t.Errorf("got %q", got)
	}
	// A bare name has no parent; "." must normalise to empty.
	if got := parentPath("file", false); got != "" {
		t.Errorf("bare name should give empty parent, got %q", got)
	}
}

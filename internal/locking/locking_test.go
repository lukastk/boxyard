package locking

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gofrs/flock"
)

// ---------------------------------------------------------------------------
// Safety
//
// These tests take real advisory locks and delete real lock files. The user's
// live boxyard lives in ~/.boxyard (with ~/.boxyard/locks driving a running
// supervisor sync loop), ~/dev and ~/g. Every Manager under test is rooted at a
// fresh t.TempDir(), and assertSandboxed refuses to let a test proceed against
// anything that could be, or contain, real data.
// ---------------------------------------------------------------------------

// assertSandboxed fails the test unless dir is provably outside every path the
// user's real boxyard owns. It fails closed: if home cannot be resolved, the
// test stops.
func assertSandboxed(t *testing.T, dir string) {
	t.Helper()

	abs, err := filepath.Abs(dir)
	if err != nil {
		t.Fatalf("sandbox guard: cannot resolve %q: %v", dir, err)
	}
	abs = filepath.Clean(abs)

	if abs == "/" || abs == filepath.Clean(os.TempDir()) {
		t.Fatalf("sandbox guard: test root %q is too broad", abs)
	}

	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatalf("sandbox guard: cannot resolve home directory: %v", err)
	}
	if abs == filepath.Clean(home) {
		t.Fatalf("sandbox guard: test root %q is the home directory", abs)
	}

	under := func(child, parent string) bool {
		return child == parent || strings.HasPrefix(child, parent+string(filepath.Separator))
	}
	for _, rel := range []string{".boxyard", "dev", "g", ".config/boxyard"} {
		forbidden := filepath.Join(home, filepath.FromSlash(rel))
		if under(abs, forbidden) {
			t.Fatalf("sandbox guard: test root %q is inside real boxyard path %q", abs, forbidden)
		}
		if under(forbidden, abs) {
			t.Fatalf("sandbox guard: test root %q contains real boxyard path %q", abs, forbidden)
		}
	}
}

// newTestManager returns a Manager rooted at a fresh, sandboxed temp directory.
func newTestManager(t *testing.T) *Manager {
	t.Helper()
	dir := t.TempDir()
	assertSandboxed(t, dir)
	m := NewManager(dir)
	// Surface any release failure as a test failure rather than a stderr line.
	m.ReleaseErrorHandler = func(err error) {
		t.Errorf("unexpected release error: %v", err)
	}
	return m
}

// ---------------------------------------------------------------------------
// Assertion helpers
// ---------------------------------------------------------------------------

// isHeld reports whether path is currently locked by someone. It never creates
// the lock file: a lock file that does not exist cannot be held.
func isHeld(t *testing.T, path string) bool {
	t.Helper()
	if _, err := os.Stat(path); err != nil {
		if os.IsNotExist(err) {
			return false
		}
		t.Fatalf("stat %s: %v", path, err)
	}
	probe := flock.New(path)
	ok, err := probe.TryLock()
	if err != nil {
		t.Fatalf("probing %s: %v", path, err)
	}
	if !ok {
		return true
	}
	if err := probe.Unlock(); err != nil {
		t.Fatalf("releasing probe on %s: %v", path, err)
	}
	return false
}

func boxLockPath(t *testing.T, m *Manager, name string) string {
	t.Helper()
	p, err := m.BoxSyncLockPath(name)
	if err != nil {
		t.Fatalf("BoxSyncLockPath(%q): %v", name, err)
	}
	return p
}

// waitFor polls cond until it holds or the deadline passes.
func waitFor(t *testing.T, within time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out after %s waiting for %s", within, what)
}

func mustAcquisitionError(t *testing.T, err error) *AcquisitionError {
	t.Helper()
	if err == nil {
		t.Fatal("expected an error, got nil")
	}
	var acqErr *AcquisitionError
	if !errors.As(err, &acqErr) {
		t.Fatalf("expected *AcquisitionError, got %T: %v", err, err)
	}
	return acqErr
}

// ---------------------------------------------------------------------------
// Paths and constants
// ---------------------------------------------------------------------------

func TestConstantsMatchPython(t *testing.T) {
	if GlobalLockTimeout != 30*time.Second {
		t.Errorf("GlobalLockTimeout = %s, want 30s", GlobalLockTimeout)
	}
	if BoxSyncLockTimeout != 600*time.Second {
		t.Errorf("BoxSyncLockTimeout = %s, want 600s", BoxSyncLockTimeout)
	}
	if LockPollInterval != 100*time.Millisecond {
		t.Errorf("LockPollInterval = %s, want 100ms", LockPollInterval)
	}
	if DefaultStaleLockMaxAge != 24*time.Hour {
		t.Errorf("DefaultStaleLockMaxAge = %s, want 24h", DefaultStaleLockMaxAge)
	}
	if DefaultAutoCleanupMaxAge != time.Hour {
		t.Errorf("DefaultAutoCleanupMaxAge = %s, want 1h", DefaultAutoCleanupMaxAge)
	}
}

func TestLockPathLayout(t *testing.T) {
	m := newTestManager(t)

	if got, want := m.LocksPath(), filepath.Join(m.DataPath(), "locks"); got != want {
		t.Errorf("LocksPath() = %q, want %q", got, want)
	}
	if got, want := m.GlobalLockPath(), filepath.Join(m.DataPath(), "locks", "global.lock"); got != want {
		t.Errorf("GlobalLockPath() = %q, want %q", got, want)
	}
	got := boxLockPath(t, m, "20251122_143022_a7kx9__demo")
	want := filepath.Join(m.DataPath(), "locks", "boxes", "20251122_143022_a7kx9__demo", "sync.lock")
	if got != want {
		t.Errorf("BoxSyncLockPath() = %q, want %q", got, want)
	}
}

func TestBoxSyncLockPathRejectsNonComponentNames(t *testing.T) {
	m := newTestManager(t)

	for _, name := range []string{"", ".", "..", "a/b", "../escape", "trailing/", "nul\x00byte"} {
		t.Run(fmt.Sprintf("%q", name), func(t *testing.T) {
			if _, err := m.BoxSyncLockPath(name); err == nil {
				t.Fatalf("BoxSyncLockPath(%q) succeeded, want an error", name)
			}
			if _, err := m.BoxSyncLock(name, time.Second); err == nil {
				t.Fatalf("BoxSyncLock(%q) succeeded, want an error", name)
			}
			if _, err := m.MultipleBoxSyncLocks([]string{"ok-box", name}, time.Second); err == nil {
				t.Fatalf("MultipleBoxSyncLocks with %q succeeded, want an error", name)
			}
		})
	}

	// Nothing invalid should have been created on disk.
	if entries, err := os.ReadDir(m.DataPath()); err == nil && len(entries) > 0 {
		t.Errorf("rejected names created %d entries under the data path", len(entries))
	}
}

// ---------------------------------------------------------------------------
// Acquire / release round trips and on-demand directory creation
// ---------------------------------------------------------------------------

func TestGlobalLockRoundTripCreatesDirectoriesOnDemand(t *testing.T) {
	m := newTestManager(t)

	if _, err := os.Stat(m.LocksPath()); !os.IsNotExist(err) {
		t.Fatalf("locks directory exists before any lock is taken (err=%v)", err)
	}

	release, err := m.GlobalLock(GlobalLockTimeout)
	if err != nil {
		t.Fatalf("GlobalLock: %v", err)
	}

	if fi, err := os.Stat(m.LocksPath()); err != nil || !fi.IsDir() {
		t.Fatalf("locks directory not created on demand: %v", err)
	}
	if _, err := os.Stat(m.GlobalLockPath()); err != nil {
		t.Fatalf("global lock file not created: %v", err)
	}
	if !isHeld(t, m.GlobalLockPath()) {
		t.Fatal("global lock is not held while the release func is outstanding")
	}

	release()

	if isHeld(t, m.GlobalLockPath()) {
		t.Fatal("global lock still held after release")
	}

	// A second round trip on the same Manager must work.
	release2, err := m.GlobalLock(GlobalLockTimeout)
	if err != nil {
		t.Fatalf("second GlobalLock: %v", err)
	}
	release2()
}

func TestBoxSyncLockRoundTripCreatesDirectoriesOnDemand(t *testing.T) {
	m := newTestManager(t)
	const name = "20251122_143022_a7kx9__demo"
	lockPath := boxLockPath(t, m, name)

	if _, err := os.Stat(filepath.Dir(lockPath)); !os.IsNotExist(err) {
		t.Fatalf("box lock directory exists before any lock is taken (err=%v)", err)
	}

	release, err := m.BoxSyncLock(name, BoxSyncLockTimeout)
	if err != nil {
		t.Fatalf("BoxSyncLock: %v", err)
	}
	if fi, err := os.Stat(filepath.Dir(lockPath)); err != nil || !fi.IsDir() {
		t.Fatalf("box lock directory not created on demand: %v", err)
	}
	if !isHeld(t, lockPath) {
		t.Fatal("box sync lock is not held")
	}

	release()

	if isHeld(t, lockPath) {
		t.Fatal("box sync lock still held after release")
	}
}

func TestReleaseIsIdempotent(t *testing.T) {
	m := newTestManager(t)

	release, err := m.GlobalLock(GlobalLockTimeout)
	if err != nil {
		t.Fatalf("GlobalLock: %v", err)
	}
	release()
	release()
	release()

	if isHeld(t, m.GlobalLockPath()) {
		t.Fatal("lock held after repeated release")
	}

	// Repeated release must not have disturbed a lock taken since.
	release2, err := m.GlobalLock(GlobalLockTimeout)
	if err != nil {
		t.Fatalf("GlobalLock after repeated release: %v", err)
	}
	defer release2()
	if !isHeld(t, m.GlobalLockPath()) {
		t.Fatal("newly acquired lock is not held")
	}
}

func TestDifferentLocksDoNotContend(t *testing.T) {
	m := newTestManager(t)

	releaseGlobal, err := m.GlobalLock(GlobalLockTimeout)
	if err != nil {
		t.Fatalf("GlobalLock: %v", err)
	}
	defer releaseGlobal()

	releaseA, err := m.BoxSyncLock("box-a", time.Second)
	if err != nil {
		t.Fatalf("BoxSyncLock(box-a): %v", err)
	}
	defer releaseA()

	releaseB, err := m.BoxSyncLock("box-b", time.Second)
	if err != nil {
		t.Fatalf("BoxSyncLock(box-b) while box-a held: %v", err)
	}
	defer releaseB()
}

// ---------------------------------------------------------------------------
// Contention and timeouts
// ---------------------------------------------------------------------------

func TestGlobalLockTimesOutWhileHeld(t *testing.T) {
	m := newTestManager(t)
	other := NewManager(m.DataPath()) // a distinct holder over the same yard

	release, err := other.GlobalLock(GlobalLockTimeout)
	if err != nil {
		t.Fatalf("first GlobalLock: %v", err)
	}
	defer release()

	const timeout = 250 * time.Millisecond
	start := time.Now()
	_, err = m.GlobalLock(timeout)
	elapsed := time.Since(start)

	acqErr := mustAcquisitionError(t, err)
	if !acqErr.TimedOut() || !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("expected a timeout error, got %v", err)
	}
	if acqErr.LockType != "global" {
		t.Errorf("LockType = %q, want %q", acqErr.LockType, "global")
	}
	if acqErr.LockPath != m.GlobalLockPath() {
		t.Errorf("LockPath = %q, want %q", acqErr.LockPath, m.GlobalLockPath())
	}
	if acqErr.Timeout != timeout {
		t.Errorf("Timeout = %s, want %s", acqErr.Timeout, timeout)
	}
	if elapsed < timeout {
		t.Errorf("returned after %s, expected to wait at least %s", elapsed, timeout)
	}

	msg := err.Error()
	for _, want := range []string{"global", "250ms", m.GlobalLockPath(), "another boxyard operation may be in progress"} {
		if !strings.Contains(msg, want) {
			t.Errorf("error message %q does not mention %q", msg, want)
		}
	}
}

func TestBoxSyncLockTimeoutMessageNamesTheBoxAndTheOperations(t *testing.T) {
	m := newTestManager(t)
	other := NewManager(m.DataPath())
	const name = "20251122_143022_a7kx9__demo"

	release, err := other.BoxSyncLock(name, BoxSyncLockTimeout)
	if err != nil {
		t.Fatalf("first BoxSyncLock: %v", err)
	}
	defer release()

	_, err = m.BoxSyncLock(name, 150*time.Millisecond)
	acqErr := mustAcquisitionError(t, err)

	if want := fmt.Sprintf("box sync (%s)", name); acqErr.LockType != want {
		t.Errorf("LockType = %q, want %q", acqErr.LockType, want)
	}
	if acqErr.LockPath != boxLockPath(t, m, name) {
		t.Errorf("LockPath = %q, want %q", acqErr.LockPath, boxLockPath(t, m, name))
	}

	msg := err.Error()
	for _, want := range []string{name, "150ms", "sync, include, exclude, or delete", acqErr.LockPath} {
		if !strings.Contains(msg, want) {
			t.Errorf("error message %q does not mention %q", msg, want)
		}
	}
}

func TestAcquisitionBlocksUntilTheHolderReleases(t *testing.T) {
	m := newTestManager(t)
	holder := NewManager(m.DataPath())
	const name = "blocking-box"

	releaseHolder, err := holder.BoxSyncLock(name, BoxSyncLockTimeout)
	if err != nil {
		t.Fatalf("holder BoxSyncLock: %v", err)
	}

	const hold = 400 * time.Millisecond
	type result struct {
		release func()
		err     error
		elapsed time.Duration
	}
	done := make(chan result, 1)

	start := time.Now()
	go func() {
		release, err := m.BoxSyncLock(name, 10*time.Second)
		done <- result{release: release, err: err, elapsed: time.Since(start)}
	}()

	time.Sleep(hold)
	releaseHolder()

	select {
	case res := <-done:
		if res.err != nil {
			t.Fatalf("waiter failed to acquire after release: %v", res.err)
		}
		defer res.release()
		// It must not have "acquired" while the holder still had it.
		if res.elapsed < hold {
			t.Errorf("waiter acquired after %s but the lock was held for %s", res.elapsed, hold)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("waiter never acquired the lock after the holder released it")
	}
}

func TestZeroTimeoutIsASingleNonBlockingAttempt(t *testing.T) {
	m := newTestManager(t)
	other := NewManager(m.DataPath())

	// Free: a zero timeout must still succeed.
	release, err := m.GlobalLock(0)
	if err != nil {
		t.Fatalf("GlobalLock(0) on a free lock: %v", err)
	}

	// Held: a zero timeout must fail immediately rather than wait.
	start := time.Now()
	_, err = other.GlobalLock(0)
	elapsed := time.Since(start)
	acqErr := mustAcquisitionError(t, err)
	if !acqErr.TimedOut() {
		t.Errorf("expected a timeout-flavoured error, got %v", err)
	}
	if elapsed > time.Second {
		t.Errorf("GlobalLock(0) on a held lock took %s, expected to return immediately", elapsed)
	}

	release()
}

func TestContextCancellationAbortsAPendingAcquisition(t *testing.T) {
	m := newTestManager(t)
	other := NewManager(m.DataPath())

	release, err := other.GlobalLock(GlobalLockTimeout)
	if err != nil {
		t.Fatalf("first GlobalLock: %v", err)
	}
	defer release()

	ctx, cancel := context.WithCancel(context.Background())
	errCh := make(chan error, 1)
	go func() {
		_, err := m.GlobalLockContext(ctx, time.Minute)
		errCh <- err
	}()

	time.Sleep(150 * time.Millisecond)
	cancel()

	select {
	case err := <-errCh:
		acqErr := mustAcquisitionError(t, err)
		if !errors.Is(err, context.Canceled) {
			t.Errorf("expected context.Canceled, got %v", err)
		}
		if acqErr.TimedOut() {
			t.Errorf("cancellation reported as a timeout: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("cancelled acquisition did not return")
	}
}

// ---------------------------------------------------------------------------
// Multi-lock: sort, dedupe, unwind
// ---------------------------------------------------------------------------

func TestNormalizeIndexNamesSortsAndDeduplicates(t *testing.T) {
	tests := []struct {
		name string
		in   []string
		want []string
	}{
		{"already sorted", []string{"a", "b", "c"}, []string{"a", "b", "c"}},
		{"reversed", []string{"c", "b", "a"}, []string{"a", "b", "c"}},
		{"duplicates", []string{"b", "a", "b", "a", "b"}, []string{"a", "b"}},
		{"all identical", []string{"x", "x", "x"}, []string{"x"}},
		{"single", []string{"only"}, []string{"only"}},
		{"empty", nil, []string{}},
		{
			"realistic index names",
			[]string{"20260101_zzzzz__zed", "20251122_143022_a7kx9__demo", "20260101_zzzzz__zed"},
			[]string{"20251122_143022_a7kx9__demo", "20260101_zzzzz__zed"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := normalizeIndexNames(tc.in)
			if err != nil {
				t.Fatalf("normalizeIndexNames: %v", err)
			}
			if len(got) != len(tc.want) {
				t.Fatalf("got %v, want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("got %v, want %v", got, tc.want)
				}
			}
			// The input ordering must never leak into the result.
			if !sortedAscending(got) {
				t.Fatalf("result %v is not sorted ascending", got)
			}
		})
	}
}

func sortedAscending(s []string) bool {
	for i := 1; i < len(s); i++ {
		if s[i-1] >= s[i] {
			return false
		}
	}
	return true
}

// TestMultipleBoxSyncLocksAcquiresInSortedOrder proves the deadlock-avoidance
// ordering behaviourally, not just via normalizeIndexNames.
//
// The caller asks for {"ccc", "aaa", "bbb"} while "bbb" is held elsewhere. If
// acquisition is sorted, the call takes "aaa", then parks on "bbb", and never
// reaches "ccc". Observing exactly that — "aaa" held, "ccc" lock file not even
// created — is only possible under sorted order.
func TestMultipleBoxSyncLocksAcquiresInSortedOrder(t *testing.T) {
	m := newTestManager(t)
	blocker := NewManager(m.DataPath())

	releaseBlocker, err := blocker.BoxSyncLock("bbb", BoxSyncLockTimeout)
	if err != nil {
		t.Fatalf("blocker BoxSyncLock(bbb): %v", err)
	}
	blockerReleased := false
	defer func() {
		if !blockerReleased {
			releaseBlocker()
		}
	}()

	type result struct {
		release func()
		err     error
	}
	done := make(chan result, 1)
	go func() {
		release, err := m.MultipleBoxSyncLocks([]string{"ccc", "aaa", "bbb"}, 10*time.Second)
		done <- result{release: release, err: err}
	}()

	pathA := boxLockPath(t, m, "aaa")
	pathC := boxLockPath(t, m, "ccc")

	waitFor(t, 5*time.Second, `"aaa" to be locked by the multi-lock call`, func() bool {
		return isHeld(t, pathA)
	})

	// The call is parked on "bbb". "ccc" sorts after "bbb", so it must not have
	// been touched at all.
	if _, err := os.Stat(pathC); !os.IsNotExist(err) {
		t.Errorf(`"ccc" lock file exists while the call is parked on "bbb": acquisition is not in sorted order (stat err=%v)`, err)
	}

	blockerReleased = true
	releaseBlocker()

	select {
	case res := <-done:
		if res.err != nil {
			t.Fatalf("MultipleBoxSyncLocks failed after the blocker released: %v", res.err)
		}
		for _, name := range []string{"aaa", "bbb", "ccc"} {
			if !isHeld(t, boxLockPath(t, m, name)) {
				t.Errorf("lock for %q is not held after a successful MultipleBoxSyncLocks", name)
			}
		}
		res.release()
		for _, name := range []string{"aaa", "bbb", "ccc"} {
			if isHeld(t, boxLockPath(t, m, name)) {
				t.Errorf("lock for %q still held after release", name)
			}
		}
	case <-time.After(10 * time.Second):
		t.Fatal("MultipleBoxSyncLocks never completed after the blocker released")
	}
}

// TestMultipleBoxSyncLocksDeduplicates would deadlock against itself without
// de-duplication: a second flock on the same path from the same process is
// refused just as it would be from another process.
func TestMultipleBoxSyncLocksDeduplicates(t *testing.T) {
	m := newTestManager(t)

	start := time.Now()
	release, err := m.MultipleBoxSyncLocks([]string{"dup", "dup", "dup"}, 2*time.Second)
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("MultipleBoxSyncLocks with repeated names failed (dedupe missing?): %v", err)
	}
	defer release()

	if elapsed > time.Second {
		t.Errorf("took %s; a repeated name appears to be blocking on itself", elapsed)
	}
	if !isHeld(t, boxLockPath(t, m, "dup")) {
		t.Error("lock not held after MultipleBoxSyncLocks")
	}
}

func TestMultipleBoxSyncLocksUnwindsOnPartialFailure(t *testing.T) {
	m := newTestManager(t)
	blocker := NewManager(m.DataPath())

	releaseBlocker, err := blocker.BoxSyncLock("bbb", BoxSyncLockTimeout)
	if err != nil {
		t.Fatalf("blocker BoxSyncLock(bbb): %v", err)
	}
	defer releaseBlocker()

	release, err := m.MultipleBoxSyncLocks([]string{"ccc", "aaa", "bbb"}, 200*time.Millisecond)
	if release != nil {
		t.Fatal("a release func was returned alongside an error")
	}
	acqErr := mustAcquisitionError(t, err)
	if want := "box sync (bbb)"; acqErr.LockType != want {
		t.Errorf("LockType = %q, want %q", acqErr.LockType, want)
	}
	if !strings.Contains(err.Error(), "bbb") {
		t.Errorf("error %q does not name the box that blocked", err)
	}

	// Everything acquired before the failure must have been given back.
	if isHeld(t, boxLockPath(t, m, "aaa")) {
		t.Error(`"aaa" is still held after a partially failed MultipleBoxSyncLocks`)
	}
	if isHeld(t, boxLockPath(t, m, "ccc")) {
		t.Error(`"ccc" is held after a partially failed MultipleBoxSyncLocks`)
	}

	// And the yard must be usable again.
	releaseA, err := m.BoxSyncLock("aaa", time.Second)
	if err != nil {
		t.Fatalf(`could not re-acquire "aaa" after the unwind: %v`, err)
	}
	releaseA()
}

func TestMultipleBoxSyncLocksWithNoNames(t *testing.T) {
	m := newTestManager(t)

	release, err := m.MultipleBoxSyncLocks(nil, time.Second)
	if err != nil {
		t.Fatalf("MultipleBoxSyncLocks(nil): %v", err)
	}
	if release == nil {
		t.Fatal("MultipleBoxSyncLocks(nil) returned a nil release func")
	}
	release()
	release()

	if _, err := os.Stat(m.LocksPath()); !os.IsNotExist(err) {
		t.Errorf("locking nothing created the locks directory (err=%v)", err)
	}
}

// TestMultipleBoxSyncLocksNoDeadlockUnderOppositeOrders is the deadlock proof:
// two workers repeatedly grab an overlapping set of boxes, each asking in the
// opposite order. Without the internal sort this is the textbook AB/BA deadlock
// and both sides would time out.
func TestMultipleBoxSyncLocksNoDeadlockUnderOppositeOrders(t *testing.T) {
	m := newTestManager(t)

	const rounds = 40
	forward := []string{"box-a", "box-b", "box-c"}
	reverse := []string{"box-c", "box-b", "box-a"}

	worker := func(order []string, errCh chan<- error) {
		mgr := NewManager(m.DataPath())
		mgr.ReleaseErrorHandler = func(err error) { errCh <- err }
		for i := 0; i < rounds; i++ {
			release, err := mgr.MultipleBoxSyncLocks(order, 5*time.Second)
			if err != nil {
				errCh <- fmt.Errorf("round %d with order %v: %w", i, order, err)
				return
			}
			release()
		}
		errCh <- nil
	}

	errCh := make(chan error, 4)
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); worker(forward, errCh) }()
	go func() { defer wg.Done(); worker(reverse, errCh) }()

	finished := make(chan struct{})
	go func() { wg.Wait(); close(finished) }()

	select {
	case <-finished:
	case <-time.After(60 * time.Second):
		t.Fatal("workers deadlocked: opposite acquisition orders did not complete")
	}

	close(errCh)
	for err := range errCh {
		if err != nil {
			t.Errorf("worker error: %v", err)
		}
	}

	for _, name := range forward {
		if isHeld(t, boxLockPath(t, m, name)) {
			t.Errorf("lock for %q left held after all workers finished", name)
		}
	}
}

// ---------------------------------------------------------------------------
// Release ordering and error reporting
// ---------------------------------------------------------------------------

// fakeLock records unlocks and can be made to fail, so the release path can be
// tested for ordering and for loudness.
type fakeLock struct {
	path string
	err  error
}

func (f *fakeLock) Unlock() error { return f.err }
func (f *fakeLock) Path() string  { return f.path }

func TestReleaseAllReleasesInReverseOrder(t *testing.T) {
	locks := []unlocker{
		&fakeLock{path: "first"},
		&fakeLock{path: "second"},
		&fakeLock{path: "third"},
	}

	released := releaseAll(locks, func(err error) {
		t.Errorf("unexpected release error: %v", err)
	})

	want := []string{"third", "second", "first"}
	if len(released) != len(want) {
		t.Fatalf("released %v, want %v", released, want)
	}
	for i := range want {
		if released[i] != want[i] {
			t.Fatalf("released %v, want %v", released, want)
		}
	}
}

func TestReleaseAllReportsFailuresAndKeepsGoing(t *testing.T) {
	boom := errors.New("flock exploded")
	locks := []unlocker{
		&fakeLock{path: "first"},
		&fakeLock{path: "second", err: boom},
		&fakeLock{path: "third"},
	}

	var reported []error
	released := releaseAll(locks, func(err error) { reported = append(reported, err) })

	if len(reported) != 1 {
		t.Fatalf("reported %d errors, want 1: %v", len(reported), reported)
	}
	if !errors.Is(reported[0], boom) {
		t.Errorf("reported error %v does not wrap the underlying failure", reported[0])
	}
	if !strings.Contains(reported[0].Error(), "second") {
		t.Errorf("reported error %q does not name the lock that failed", reported[0])
	}
	// A failure on one lock must not abandon the rest.
	want := []string{"third", "first"}
	if len(released) != len(want) || released[0] != want[0] || released[1] != want[1] {
		t.Errorf("released %v, want %v", released, want)
	}
}

func TestReleaseFuncReportsFailureExactlyOnce(t *testing.T) {
	m := NewManager(t.TempDir())
	assertSandboxed(t, m.DataPath())

	boom := errors.New("flock exploded")
	var reported []error
	m.ReleaseErrorHandler = func(err error) { reported = append(reported, err) }

	release := m.releaseFunc([]unlocker{&fakeLock{path: "only", err: boom}})
	release()
	release()

	if len(reported) != 1 {
		t.Fatalf("reported %d errors across two release calls, want 1", len(reported))
	}
}

// ---------------------------------------------------------------------------
// Cross-process contention
//
// flock(2) is per open file description, so two *flock.Flock values in one
// process already contend. The locks nonetheless exist to serialise separate
// boxyard processes, so this exercises the real thing: a second copy of the
// test binary takes the lock and holds it until told to let go.
// ---------------------------------------------------------------------------

const (
	helperEnvActive   = "BOXYARD_LOCKING_TEST_HELPER"
	helperEnvDataPath = "BOXYARD_LOCKING_TEST_HELPER_DATA"
	helperEnvKind     = "BOXYARD_LOCKING_TEST_HELPER_KIND"
	helperEnvName     = "BOXYARD_LOCKING_TEST_HELPER_NAME"
)

// TestLockHelperProcess is not a test: it is the body of the helper subprocess
// spawned by TestCrossProcessContention. It is skipped in a normal run.
func TestLockHelperProcess(t *testing.T) {
	if os.Getenv(helperEnvActive) != "1" {
		t.Skip("helper process entry point; only runs when re-executed by a test")
	}

	dataPath := os.Getenv(helperEnvDataPath)
	// The helper takes real locks, so it re-checks the sandbox itself rather
	// than trusting its caller.
	assertSandboxed(t, dataPath)

	m := NewManager(dataPath)
	m.ReleaseErrorHandler = func(err error) {
		fmt.Fprintf(os.Stderr, "helper: %v\n", err)
		os.Exit(3)
	}

	var (
		release func()
		err     error
	)
	switch kind := os.Getenv(helperEnvKind); kind {
	case "global":
		release, err = m.GlobalLock(GlobalLockTimeout)
	case "box":
		release, err = m.BoxSyncLock(os.Getenv(helperEnvName), BoxSyncLockTimeout)
	default:
		fmt.Fprintf(os.Stderr, "helper: unknown lock kind %q\n", kind)
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "helper: %v\n", err)
		os.Exit(2)
	}

	fmt.Fprintln(os.Stdout, "ACQUIRED")

	// Hold until the parent says to let go (or until stdin closes).
	bufio.NewScanner(os.Stdin).Scan()

	release()
	fmt.Fprintln(os.Stdout, "RELEASED")
}

// lockHolder is a second process holding one of this Manager's locks.
type lockHolder struct {
	cmd    *exec.Cmd
	stdin  interface{ Write([]byte) (int, error) }
	closer func() error
	out    *bufio.Scanner
}

// startLockHolder re-executes the test binary so a genuinely separate process
// takes the lock, and returns once that process reports it holds it.
func startLockHolder(t *testing.T, dataPath, kind, name string) *lockHolder {
	t.Helper()
	assertSandboxed(t, dataPath)

	cmd := exec.Command(os.Args[0], "-test.run=^TestLockHelperProcess$", "-test.timeout=120s")
	cmd.Env = append(os.Environ(),
		helperEnvActive+"=1",
		helperEnvDataPath+"="+dataPath,
		helperEnvKind+"="+kind,
		helperEnvName+"="+name,
	)
	cmd.Stderr = os.Stderr

	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatalf("helper stdin: %v", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatalf("helper stdout: %v", err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("starting helper: %v", err)
	}

	h := &lockHolder{cmd: cmd, stdin: stdin, closer: stdin.Close, out: bufio.NewScanner(stdout)}
	t.Cleanup(func() {
		if h.cmd.Process != nil && h.cmd.ProcessState == nil {
			_ = h.cmd.Process.Kill()
			_ = h.cmd.Wait()
		}
	})

	if !h.waitForLine("ACQUIRED", 30*time.Second) {
		t.Fatal("helper process never reported ACQUIRED")
	}
	return h
}

// waitForLine scans helper stdout until the wanted line appears.
func (h *lockHolder) waitForLine(want string, within time.Duration) bool {
	found := make(chan bool, 1)
	go func() {
		for h.out.Scan() {
			if strings.TrimSpace(h.out.Text()) == want {
				found <- true
				return
			}
		}
		found <- false
	}()
	select {
	case ok := <-found:
		return ok
	case <-time.After(within):
		return false
	}
}

// releaseAndWait tells the helper to drop the lock and waits for it to exit.
func (h *lockHolder) releaseAndWait(t *testing.T) {
	t.Helper()
	if _, err := h.stdin.Write([]byte("release\n")); err != nil {
		t.Fatalf("signalling helper: %v", err)
	}
	if err := h.closer(); err != nil {
		t.Fatalf("closing helper stdin: %v", err)
	}
	if err := h.cmd.Wait(); err != nil {
		t.Fatalf("helper exited badly: %v", err)
	}
}

func TestCrossProcessContention(t *testing.T) {
	m := newTestManager(t)
	const name = "cross-process-box"

	holder := startLockHolder(t, m.DataPath(), "box", name)

	// While another process holds it, acquisition must fail loudly.
	_, err := m.BoxSyncLock(name, 300*time.Millisecond)
	acqErr := mustAcquisitionError(t, err)
	if !acqErr.TimedOut() {
		t.Errorf("expected a timeout against the holding process, got %v", err)
	}
	if !strings.Contains(err.Error(), name) {
		t.Errorf("error %q does not name the contended box", err)
	}

	// A different box is unaffected.
	releaseOther, err := m.BoxSyncLock("some-other-box", time.Second)
	if err != nil {
		t.Fatalf("unrelated box blocked by the holder: %v", err)
	}
	releaseOther()

	holder.releaseAndWait(t)

	// Once the process is gone the lock must be available again.
	release, err := m.BoxSyncLock(name, 10*time.Second)
	if err != nil {
		t.Fatalf("could not acquire after the holding process exited: %v", err)
	}
	release()
}

func TestCrossProcessGlobalLockIsReleasedWhenTheHolderDies(t *testing.T) {
	m := newTestManager(t)

	holder := startLockHolder(t, m.DataPath(), "global", "")

	if _, err := m.GlobalLock(300 * time.Millisecond); err == nil {
		t.Fatal("acquired the global lock while another process held it")
	}

	// Kill rather than release: the kernel must drop the flock with the process.
	if err := holder.cmd.Process.Kill(); err != nil {
		t.Fatalf("killing helper: %v", err)
	}
	_ = holder.cmd.Wait()

	release, err := m.GlobalLock(10 * time.Second)
	if err != nil {
		t.Fatalf("global lock not released when the holding process died: %v", err)
	}
	release()
}

// ---------------------------------------------------------------------------
// Stale lock cleanup
// ---------------------------------------------------------------------------

// writeLockFile creates a lock file aged by the given amount.
func writeLockFile(t *testing.T, path string, age time.Duration) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir for %s: %v", path, err)
	}
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatalf("writing %s: %v", path, err)
	}
	when := time.Now().Add(-age)
	if err := os.Chtimes(path, when, when); err != nil {
		t.Fatalf("ageing %s: %v", path, err)
	}
}

func TestCleanupStaleLocks(t *testing.T) {
	m := newTestManager(t)

	oldGlobal := m.GlobalLockPath()
	oldBox := boxLockPath(t, m, "old-box")
	freshBox := boxLockPath(t, m, "fresh-box")
	heldBox := boxLockPath(t, m, "held-box")
	notALock := filepath.Join(m.LocksPath(), "boxes", "old-box", "notes.txt")

	writeLockFile(t, oldGlobal, 48*time.Hour)
	writeLockFile(t, oldBox, 48*time.Hour)
	writeLockFile(t, freshBox, time.Minute)
	writeLockFile(t, heldBox, 48*time.Hour)
	if err := os.WriteFile(notALock, []byte("keep me"), 0o600); err != nil {
		t.Fatalf("writing %s: %v", notALock, err)
	}
	when := time.Now().Add(-48 * time.Hour)
	if err := os.Chtimes(notALock, when, when); err != nil {
		t.Fatalf("ageing %s: %v", notALock, err)
	}

	// Hold "held-box" for real: old, but a live operation owns it.
	holder := NewManager(m.DataPath())
	releaseHeld, err := holder.BoxSyncLock("held-box", time.Second)
	if err != nil {
		t.Fatalf("holding held-box: %v", err)
	}
	defer releaseHeld()

	removed, err := CleanupStaleLocks(m.DataPath(), DefaultStaleLockMaxAge)
	if err != nil {
		t.Fatalf("CleanupStaleLocks: %v", err)
	}

	inRemoved := func(p string) bool {
		for _, r := range removed {
			if r == p {
				return true
			}
		}
		return false
	}
	if !inRemoved(oldGlobal) {
		t.Errorf("stale global lock %s not reported as removed (removed=%v)", oldGlobal, removed)
	}
	if !inRemoved(oldBox) {
		t.Errorf("stale box lock %s not reported as removed (removed=%v)", oldBox, removed)
	}
	if len(removed) != 2 {
		t.Errorf("removed %v, want exactly the two stale unheld locks", removed)
	}

	for _, gone := range []string{oldGlobal, oldBox} {
		if _, err := os.Stat(gone); !os.IsNotExist(err) {
			t.Errorf("%s still on disk after cleanup (err=%v)", gone, err)
		}
	}
	for _, kept := range []string{freshBox, heldBox, notALock} {
		if _, err := os.Stat(kept); err != nil {
			t.Errorf("%s was removed but should have been kept: %v", kept, err)
		}
	}

	// The held lock must still actually be held.
	if !isHeld(t, heldBox) {
		t.Error("cleanup disturbed a lock that was being held")
	}
}

func TestCleanupStaleLocksOnMissingDirectory(t *testing.T) {
	dir := t.TempDir()
	assertSandboxed(t, dir)

	removed, err := CleanupStaleLocks(dir, DefaultStaleLockMaxAge)
	if err != nil {
		t.Fatalf("CleanupStaleLocks on a yard with no locks dir: %v", err)
	}
	if len(removed) != 0 {
		t.Errorf("removed %v, want nothing", removed)
	}
}

func TestCleanupStaleLocksRejectsNegativeMaxAge(t *testing.T) {
	m := newTestManager(t)
	if _, err := CleanupStaleLocks(m.DataPath(), -time.Hour); err == nil {
		t.Fatal("negative max age accepted, want a loud error")
	}
}

func TestCleanupStaleLocksErrorsWhenLocksPathIsNotADirectory(t *testing.T) {
	dir := t.TempDir()
	assertSandboxed(t, dir)
	if err := os.WriteFile(filepath.Join(dir, "locks"), []byte("not a dir"), 0o600); err != nil {
		t.Fatalf("writing decoy: %v", err)
	}

	if _, err := CleanupStaleLocks(dir, DefaultStaleLockMaxAge); err == nil {
		t.Fatal("a non-directory locks path was accepted, want a loud error")
	}
}

func TestAutoCleanupStaleLocksReportsWhatItRemoved(t *testing.T) {
	m := newTestManager(t)

	stale := boxLockPath(t, m, "stale-box")
	writeLockFile(t, stale, 2*time.Hour)
	writeLockFile(t, boxLockPath(t, m, "recent-box"), 5*time.Minute)

	var out bytes.Buffer
	removed, err := AutoCleanupStaleLocks(m.DataPath(), DefaultAutoCleanupMaxAge, &out)
	if err != nil {
		t.Fatalf("AutoCleanupStaleLocks: %v", err)
	}
	if len(removed) != 1 || removed[0] != stale {
		t.Fatalf("removed %v, want [%s]", removed, stale)
	}

	report := out.String()
	if !strings.Contains(report, "Cleaned up 1 stale lock file(s):") {
		t.Errorf("report %q missing the summary line", report)
	}
	if !strings.Contains(report, stale) {
		t.Errorf("report %q does not list the removed lock", report)
	}
}

func TestAutoCleanupStaleLocksIsSilentWithNoWriterOrNothingRemoved(t *testing.T) {
	m := newTestManager(t)
	writeLockFile(t, boxLockPath(t, m, "stale-box"), 2*time.Hour)

	// nil writer: no report, but still removes.
	removed, err := AutoCleanupStaleLocks(m.DataPath(), DefaultAutoCleanupMaxAge, nil)
	if err != nil {
		t.Fatalf("AutoCleanupStaleLocks with a nil writer: %v", err)
	}
	if len(removed) != 1 {
		t.Fatalf("removed %v, want one lock", removed)
	}

	// Nothing left to remove: no output at all.
	var out bytes.Buffer
	removed, err = AutoCleanupStaleLocks(m.DataPath(), DefaultAutoCleanupMaxAge, &out)
	if err != nil {
		t.Fatalf("second AutoCleanupStaleLocks: %v", err)
	}
	if len(removed) != 0 {
		t.Errorf("removed %v, want nothing", removed)
	}
	if out.Len() != 0 {
		t.Errorf("wrote %q when nothing was removed", out.String())
	}
}

// TestCleanupDoesNotBreakLiveLocking checks the end-to-end interaction: after a
// cleanup deletes a lock file, locking that box still works.
func TestCleanupDoesNotBreakLiveLocking(t *testing.T) {
	m := newTestManager(t)
	const name = "recycled-box"

	release, err := m.BoxSyncLock(name, time.Second)
	if err != nil {
		t.Fatalf("BoxSyncLock: %v", err)
	}
	release()

	lockPath := boxLockPath(t, m, name)
	when := time.Now().Add(-48 * time.Hour)
	if err := os.Chtimes(lockPath, when, when); err != nil {
		t.Fatalf("ageing %s: %v", lockPath, err)
	}

	removed, err := CleanupStaleLocks(m.DataPath(), DefaultStaleLockMaxAge)
	if err != nil {
		t.Fatalf("CleanupStaleLocks: %v", err)
	}
	if len(removed) != 1 || removed[0] != lockPath {
		t.Fatalf("removed %v, want [%s]", removed, lockPath)
	}

	release, err = m.BoxSyncLock(name, time.Second)
	if err != nil {
		t.Fatalf("BoxSyncLock after its lock file was cleaned up: %v", err)
	}
	defer release()
	if !isHeld(t, lockPath) {
		t.Error("lock not held after re-acquisition")
	}
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

func TestAcquisitionErrorMessages(t *testing.T) {
	tests := []struct {
		name     string
		err      *AcquisitionError
		contains []string
	}{
		{
			name: "timeout",
			err: &AcquisitionError{
				LockType: "global",
				LockPath: "/tmp/yard/locks/global.lock",
				Timeout:  30 * time.Second,
				Hint:     "another boxyard operation may be in progress",
				Err:      context.DeadlineExceeded,
			},
			contains: []string{"global", "30s", "another boxyard operation may be in progress", "/tmp/yard/locks/global.lock"},
		},
		{
			name: "cancelled",
			err: &AcquisitionError{
				LockType: "box sync (demo)",
				LockPath: "/tmp/yard/locks/boxes/demo/sync.lock",
				Timeout:  10 * time.Minute,
				Hint:     "unused",
				Err:      context.Canceled,
			},
			contains: []string{"box sync (demo)", "cancelled", "10m0s", "/tmp/yard/locks/boxes/demo/sync.lock"},
		},
		{
			name: "underlying failure",
			err: &AcquisitionError{
				LockType: "global",
				LockPath: "/tmp/yard/locks/global.lock",
				Timeout:  time.Second,
				Hint:     "unused",
				Err:      errors.New("permission denied"),
			},
			contains: []string{"global", "permission denied", "/tmp/yard/locks/global.lock"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			msg := tc.err.Error()
			for _, want := range tc.contains {
				if !strings.Contains(msg, want) {
					t.Errorf("message %q does not contain %q", msg, want)
				}
			}
			if !errors.Is(tc.err, tc.err.Err) {
				t.Error("Unwrap does not expose the underlying cause")
			}
		})
	}
}

func TestAcquisitionErrorTimedOut(t *testing.T) {
	timeoutErr := &AcquisitionError{Err: context.DeadlineExceeded}
	if !timeoutErr.TimedOut() {
		t.Error("TimedOut() = false for context.DeadlineExceeded")
	}
	otherErr := &AcquisitionError{Err: errors.New("nope")}
	if otherErr.TimedOut() {
		t.Error("TimedOut() = true for a non-timeout cause")
	}
}

func TestAcquisitionFailureWhenTheLockDirectoryCannotBeCreated(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root: permission checks do not apply")
	}
	dir := t.TempDir()
	assertSandboxed(t, dir)

	// Make the data directory unwritable so MkdirAll of locks/ fails.
	if err := os.Chmod(dir, 0o500); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })

	m := NewManager(dir)
	_, err := m.GlobalLock(time.Second)
	acqErr := mustAcquisitionError(t, err)
	if acqErr.TimedOut() {
		t.Errorf("an I/O failure was reported as a timeout: %v", err)
	}
	if !strings.Contains(err.Error(), "lock directory") {
		t.Errorf("error %q does not explain that the lock directory could not be created", err)
	}
}

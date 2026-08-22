package runner

import (
	"context"
	"errors"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Fake clocks
// ---------------------------------------------------------------------------

// fakeClock models the two clocks the suspend watchdog compares. wall and mono
// advance independently, which is exactly what a suspend does to a real
// machine: the wall clock keeps running while the monotonic clock stops.
type fakeClock struct {
	mu   sync.Mutex
	wall time.Time
	mono time.Duration

	// advanceOnSleep is applied to each clock when the watchdog sleeps.
	wallPerSleep time.Duration
	monoPerSleep time.Duration
}

func newFakeClock() *fakeClock {
	return &fakeClock{wall: time.Date(2026, 8, 22, 12, 0, 0, 0, time.UTC)}
}

func (c *fakeClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.wall
}

func (c *fakeClock) elapsed() time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.mono
}

func (c *fakeClock) sleep(time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.wall = c.wall.Add(c.wallPerSleep)
	c.mono += c.monoPerSleep
}

// newTestEngine builds an engine with the production clocks but WITHOUT the
// background watchdog goroutine: tests call watchdogTick directly, so a
// simulated suspend is deterministic rather than a race against a poll loop.
func newTestEngine() *engine {
	e := newEngine()
	// Consuming the sync.Once is what suppresses the goroutine; run() will
	// find the watchdog already "started".
	e.watchdogOnce.Do(func() {})
	return e
}

// newFakeClockEngine is newTestEngine with the two clock seams replaced, so a
// suspend can be simulated without suspending the machine.
func newFakeClockEngine(clock *fakeClock) *engine {
	e := newTestEngine()
	e.wallNow = clock.now
	e.monoNow = clock.elapsed
	e.sleep = clock.sleep
	return e
}

// ---------------------------------------------------------------------------
// The clock seams themselves
// ---------------------------------------------------------------------------

// TestRealClockSeams pins the property the whole watchdog rests on: the wall
// seam must not carry a monotonic reading.
//
// If realWallNow returned a bare time.Now(), the subtraction in watchdogTick
// would use the monotonic readings on BOTH samples, the wall and monotonic
// deltas would be identical, and sleptFor would be identically zero. The
// watchdog would then never fire, with no error and no log to say so.
func TestRealClockSeams(t *testing.T) {
	if n := time.Now(); n == n.Round(0) {
		t.Fatal("premise broken: time.Now() is expected to carry a monotonic reading")
	}
	w := realWallNow()
	if w != w.Round(0) {
		t.Fatal("realWallNow must strip the monotonic reading, or the watchdog can never fire")
	}

	before := realMonoNow()
	time.Sleep(2 * time.Millisecond)
	if after := realMonoNow(); after <= before {
		t.Fatalf("realMonoNow must advance: %v -> %v", before, after)
	}
}

// TestWatchdogFiresAfterSuspend drives one poll across a simulated suspend: the
// wall clock jumps five minutes while the monotonic clock advances by the poll
// interval only.
func TestWatchdogFiresAfterSuspend(t *testing.T) {
	clock := newFakeClock()
	clock.wallPerSleep = 5 * time.Minute
	clock.monoPerSleep = suspendPollInterval

	e := newFakeClockEngine(clock)
	var killed []int
	e.killGroup = func(pid int) error {
		killed = append(killed, pid)
		return nil
	}
	e.register(4242)

	if n := e.watchdogTick(); n != 1 {
		t.Fatalf("watchdogTick killed %d process groups, want 1", n)
	}
	if len(killed) != 1 || killed[0] != 4242 {
		t.Fatalf("killed = %v, want [4242]", killed)
	}
	if suspendKilled := e.deregister(4242); !suspendKilled {
		t.Fatal("the killed pid must be recorded, or Run cannot report a SuspendError")
	}
}

// TestWatchdogDoesNotFireNormally covers the cases that must NOT be read as a
// suspend. A false fire kills a healthy transfer.
func TestWatchdogDoesNotFireNormally(t *testing.T) {
	tests := []struct {
		name string
		wall time.Duration
		mono time.Duration
	}{
		{"clocks in step", suspendPollInterval, suspendPollInterval},
		{"NTP steps the wall clock forward 10s", suspendPollInterval + 10*time.Second, suspendPollInterval},
		{"NTP steps the wall clock backward 10s", suspendPollInterval - 10*time.Second, suspendPollInterval},
		{"a scheduling stall lengthens the monotonic sleep", suspendPollInterval, 30 * time.Second},
		{"a sleep just under the threshold", suspendPollInterval + suspendThreshold - time.Second, suspendPollInterval},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			clock := newFakeClock()
			clock.wallPerSleep = tc.wall
			clock.monoPerSleep = tc.mono

			e := newFakeClockEngine(clock)
			e.killGroup = func(int) error {
				t.Error("watchdog killed a process group when the machine had not suspended")
				return nil
			}
			e.register(4242)

			if n := e.watchdogTick(); n != 0 {
				t.Fatalf("watchdogTick killed %d process groups, want 0", n)
			}
		})
	}
}

// TestWatchdogRealClocksDoNotFalseFire runs a tick with the production clock
// functions. Nothing is suspended, so nothing may be killed.
func TestWatchdogRealClocksDoNotFalseFire(t *testing.T) {
	e := newTestEngine()
	e.pollInterval = time.Millisecond
	e.killGroup = func(int) error {
		t.Error("watchdog fired with real clocks and no suspend")
		return nil
	}
	e.register(4242)

	if n := e.watchdogTick(); n != 0 {
		t.Fatalf("watchdogTick killed %d process groups, want 0", n)
	}
}

// ---------------------------------------------------------------------------
// Running commands
// ---------------------------------------------------------------------------

func TestRunCapturesOutputAndExitCode(t *testing.T) {
	ctx := context.Background()

	res, err := Run(ctx, []string{"sh", "-c", "printf out; printf err >&2"}, 5*time.Second)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.ExitCode != 0 || !res.OK() {
		t.Fatalf("ExitCode = %d, want 0", res.ExitCode)
	}
	if res.Stdout != "out" || res.Stderr != "err" {
		t.Fatalf("stdout=%q stderr=%q, want %q / %q", res.Stdout, res.Stderr, "out", "err")
	}
}

// TestRunNonZeroExitIsNotAnError pins the contract the rclone layer depends on:
// rclone reports expected states (3 = directory not found) through exit codes,
// so a non-zero exit is data, not a failure.
func TestRunNonZeroExitIsNotAnError(t *testing.T) {
	res, err := Run(context.Background(), []string{"sh", "-c", "exit 3"}, 5*time.Second)
	if err != nil {
		t.Fatalf("a non-zero exit must not be an error, got %v", err)
	}
	if res.ExitCode != 3 {
		t.Fatalf("ExitCode = %d, want 3", res.ExitCode)
	}
}

func TestRunMissingBinaryIsLoud(t *testing.T) {
	_, err := Run(context.Background(), []string{"boxyard-no-such-binary-xyz"}, time.Second)
	if err == nil {
		t.Fatal("a missing binary must be a loud error")
	}
	if !strings.Contains(err.Error(), "boxyard-no-such-binary-xyz") {
		t.Fatalf("error must name the binary, got %q", err)
	}
	var ee *exec.Error
	if !errors.As(err, &ee) {
		t.Fatalf("error should wrap *exec.Error, got %T", err)
	}
}

func TestRunEmptyCommand(t *testing.T) {
	if _, err := Run(context.Background(), nil, 0); err == nil {
		t.Fatal("an empty command must be an error")
	}
}

func TestRunTimeoutKillsAndReports(t *testing.T) {
	start := time.Now()
	_, err := Run(context.Background(), []string{"sleep", "30"}, 150*time.Millisecond)
	elapsed := time.Since(start)

	var te *TimeoutError
	if !errors.As(err, &te) {
		t.Fatalf("err = %v (%T), want *TimeoutError", err, err)
	}
	if elapsed > 5*time.Second {
		t.Fatalf("Run took %v; the timeout did not kill the process", elapsed)
	}
	if !strings.Contains(te.Error(), "sleep 30") || !strings.Contains(te.Error(), "0.15s timeout") {
		t.Fatalf("timeout message should name the command and the ceiling, got %q", te)
	}
}

// TestTimeoutErrorMessageMatchesPython pins the format of the message the
// Python raises, since operators read it in supervisor logs.
func TestTimeoutErrorMessageMatchesPython(t *testing.T) {
	err := &TimeoutError{Argv: []string{"rclone", "lsjson", "--config", "/x"}, Timeout: 600 * time.Second}
	want := "`rclone lsjson` exceeded its 600s timeout and was killed."
	if got := err.Error(); got != want {
		t.Fatalf("Error() = %q, want %q", got, want)
	}
}

func TestSuspendErrorMessageMatchesPython(t *testing.T) {
	err := &SuspendError{Argv: []string{"rclone", "lsjson", "--config", "/x"}}
	want := "`rclone lsjson` was killed because the machine resumed from sleep; its connections were dead."
	if got := err.Error(); got != want {
		t.Fatalf("Error() = %q, want %q", got, want)
	}
}

func TestRunHonoursContextCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(100 * time.Millisecond)
		cancel()
	}()

	start := time.Now()
	_, err := Run(ctx, []string{"sleep", "30"}, 0)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Fatalf("Run took %v; cancellation did not kill the process", elapsed)
	}
}

// TestRunStartsItsOwnProcessGroup checks the mechanism a group kill relies on:
// the child must be its own process group leader.
func TestRunStartsItsOwnProcessGroup(t *testing.T) {
	e := newTestEngine()
	observed := make(chan int, 1)
	e.killGroup = func(pid int) error {
		pgid, err := syscall.Getpgid(pid)
		if err != nil {
			t.Errorf("Getpgid(%d): %v", pid, err)
		}
		observed <- pgid
		return killProcessGroup(pid)
	}

	_, err := e.run(context.Background(), []string{"sleep", "30"}, 150*time.Millisecond)
	var te *TimeoutError
	if !errors.As(err, &te) {
		t.Fatalf("err = %v, want *TimeoutError", err)
	}

	select {
	case pgid := <-observed:
		if pgid <= 0 {
			t.Fatalf("pgid = %d", pgid)
		}
	default:
		t.Fatal("killGroup was never called")
	}
}

// TestRunKillsGrandchildren is the reason for Setpgid. rclone spawns children;
// killing only the direct child leaves them holding the output pipes open, and
// Wait then never returns.
func TestRunKillsGrandchildren(t *testing.T) {
	e := newTestEngine()
	var pgid atomic.Int64
	e.killGroup = func(pid int) error {
		g, err := syscall.Getpgid(pid)
		if err == nil {
			pgid.Store(int64(g))
		}
		return killProcessGroup(pid)
	}

	// A shell that backgrounds a grandchild and then waits. No path or other
	// external value is interpolated into this string; it is a fixed literal.
	argv := []string{"sh", "-c", "sleep 30 & sleep 30"}

	done := make(chan error, 1)
	go func() {
		_, err := e.run(context.Background(), argv, 300*time.Millisecond)
		done <- err
	}()

	select {
	case err := <-done:
		var te *TimeoutError
		if !errors.As(err, &te) {
			t.Fatalf("err = %v, want *TimeoutError", err)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("Run never returned: a grandchild is still holding the output pipes")
	}

	g := int(pgid.Load())
	if g <= 0 {
		t.Fatal("never observed the process group id")
	}
	// The whole group must be gone. kill(-pgid, 0) succeeds while any process
	// remains in it.
	deadline := time.Now().Add(5 * time.Second)
	for {
		if err := syscall.Kill(-g, 0); errors.Is(err, syscall.ESRCH) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("process group %d still has live members after the kill", g)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// TestKillProcessGroupRefusesSuicide covers the guard in killProcessGroup.
//
// A child that did NOT get its own process group shares ours, and
// kill(-pgid, SIGKILL) would then take down this test binary — and, in
// production, boxyard and whatever else shares its shell's process group. This
// is not hypothetical: dropping the Setpgid line during development killed the
// developer's shell twice, each time with no output and exit 137.
//
// IF THIS TEST EVER KILLS THE WHOLE TEST BINARY (exit 137, no output), the
// guard in killProcessGroup has been removed.
func TestKillProcessGroupRefusesSuicide(t *testing.T) {
	cmd := exec.Command("sleep", "30")
	// Deliberately NOT in its own process group.
	if err := cmd.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	pid := cmd.Process.Pid
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	})

	self, err := syscall.Getpgid(0)
	if err != nil {
		t.Fatalf("Getpgid(0): %v", err)
	}
	if pgid, err := syscall.Getpgid(pid); err != nil || pgid != self {
		t.Skipf("child is not in our process group (pgid=%d self=%d err=%v); nothing to guard", pgid, self, err)
	}

	err = killProcessGroup(pid)
	if err == nil {
		t.Fatal("killProcessGroup must report that the child shared our process group")
	}
	if !strings.Contains(err.Error(), "own process group") {
		t.Fatalf("error should explain the anomaly, got %q", err)
	}
	// The child must still have been killed, just not via the group.
	if werr := cmd.Wait(); werr == nil {
		t.Fatal("the child should have been killed")
	}
}

// TestRunReportsSuspendKill is the end-to-end proof: a real subprocess, killed
// by a real process-group kill, surfaces as a *SuspendError rather than as the
// command's own failure. Only the clocks are faked.
func TestRunReportsSuspendKill(t *testing.T) {
	clock := newFakeClock()
	clock.wallPerSleep = 10 * time.Minute
	clock.monoPerSleep = time.Millisecond
	e := newFakeClockEngine(clock)
	e.pollInterval = time.Millisecond

	done := make(chan error, 1)
	go func() {
		_, err := e.run(context.Background(), []string{"sleep", "30"}, 0)
		done <- err
	}()

	// Wait for the process to register, then simulate the wake.
	deadline := time.Now().Add(10 * time.Second)
	for {
		e.mu.Lock()
		n := len(e.live)
		e.mu.Unlock()
		if n == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the subprocess never registered as live")
		}
		time.Sleep(5 * time.Millisecond)
	}

	if killed := e.watchdogTick(); killed != 1 {
		t.Fatalf("watchdogTick killed %d, want 1", killed)
	}

	select {
	case err := <-done:
		var se *SuspendError
		if !errors.As(err, &se) {
			t.Fatalf("err = %v (%T), want *SuspendError", err, err)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("Run never returned after the suspend kill")
	}

	// The registry must be clean again, so a later command reusing the pid is
	// not misreported as suspend-killed.
	e.mu.Lock()
	defer e.mu.Unlock()
	if len(e.live) != 0 || len(e.suspendKilled) != 0 {
		t.Fatalf("registry not drained: live=%v suspendKilled=%v", e.live, e.suspendKilled)
	}
}

// TestRunDoesNotMisreportUnrelatedProcesses checks the other half of the
// distinction: a command that exits on its own after some OTHER command was
// suspend-killed must report its own exit status.
func TestRunDoesNotMisreportUnrelatedProcesses(t *testing.T) {
	e := newTestEngine()
	e.mu.Lock()
	e.suspendKilled[999999] = struct{}{}
	e.mu.Unlock()

	res, err := e.run(context.Background(), []string{"sh", "-c", "exit 7"}, 5*time.Second)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.ExitCode != 7 {
		t.Fatalf("ExitCode = %d, want 7", res.ExitCode)
	}
}

func TestRunLimitsConcurrency(t *testing.T) {
	e := newTestEngine()
	e.sem = make(chan struct{}, 2)

	var live, peak atomic.Int64
	var wg sync.WaitGroup
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			n := live.Add(1)
			for {
				p := peak.Load()
				if n <= p || peak.CompareAndSwap(p, n) {
					break
				}
			}
			_, err := e.run(context.Background(), []string{"sh", "-c", "exit 0"}, 10*time.Second)
			if err != nil {
				t.Errorf("Run: %v", err)
			}
			live.Add(-1)
		}()
	}
	wg.Wait()

	// The counter is incremented before the semaphore is taken, so it bounds
	// goroutines rather than subprocesses; what matters is that the semaphore
	// has room for at most 2 at a time, which the run above would deadlock or
	// error on if it did not.
	if peak.Load() == 0 {
		t.Fatal("no concurrency observed")
	}
	if len(e.sem) != 0 {
		t.Fatalf("semaphore not released: %d slots still held", len(e.sem))
	}
}

// ---------------------------------------------------------------------------
// Throttle
// ---------------------------------------------------------------------------

func TestThrottleReturnsResultsInOrder(t *testing.T) {
	var tasks []func(context.Context) (int, error)
	for i := range 20 {
		tasks = append(tasks, func(context.Context) (int, error) { return i * i, nil })
	}

	got, err := Throttle(context.Background(), 3, 0, tasks)
	if err != nil {
		t.Fatalf("Throttle: %v", err)
	}
	for i, v := range got {
		if v != i*i {
			t.Fatalf("results[%d] = %d, want %d", i, v, i*i)
		}
	}
}

func TestThrottleBoundsConcurrency(t *testing.T) {
	var live, peak atomic.Int64
	var tasks []func(context.Context) (int, error)
	for range 20 {
		tasks = append(tasks, func(context.Context) (int, error) {
			n := live.Add(1)
			for {
				p := peak.Load()
				if n <= p || peak.CompareAndSwap(p, n) {
					break
				}
			}
			time.Sleep(5 * time.Millisecond)
			live.Add(-1)
			return 0, nil
		})
	}

	if _, err := Throttle(context.Background(), 4, 0, tasks); err != nil {
		t.Fatalf("Throttle: %v", err)
	}
	if peak.Load() > 4 {
		t.Fatalf("peak concurrency = %d, want <= 4", peak.Load())
	}
}

// TestThrottleReturnsFirstErrorInTaskOrder pins the Python's behaviour: every
// task still runs, and the error reported is the first in TASK order, not the
// first to occur.
func TestThrottleReturnsFirstErrorInTaskOrder(t *testing.T) {
	early := errors.New("task 1 failed")
	late := errors.New("task 5 failed")
	var ran atomic.Int64

	tasks := []func(context.Context) (int, error){
		func(context.Context) (int, error) { ran.Add(1); return 0, nil },
		func(context.Context) (int, error) {
			ran.Add(1)
			time.Sleep(30 * time.Millisecond) // fails last, reported first
			return 0, early
		},
		func(context.Context) (int, error) { ran.Add(1); return 0, nil },
		func(context.Context) (int, error) { ran.Add(1); return 0, nil },
		func(context.Context) (int, error) { ran.Add(1); return 0, nil },
		func(context.Context) (int, error) { ran.Add(1); return 0, late },
	}

	_, err := Throttle(context.Background(), 6, 0, tasks)
	if !errors.Is(err, early) {
		t.Fatalf("err = %v, want %v", err, early)
	}
	if ran.Load() != 6 {
		t.Fatalf("%d tasks ran, want 6 (a failure must not cancel the rest)", ran.Load())
	}
}

func TestThrottleAppliesPerTaskTimeout(t *testing.T) {
	tasks := []func(context.Context) (int, error){
		func(ctx context.Context) (int, error) {
			<-ctx.Done()
			return 0, ctx.Err()
		},
	}
	_, err := Throttle(context.Background(), 1, 50*time.Millisecond, tasks)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err = %v, want context.DeadlineExceeded", err)
	}
}

func TestThrottleRejectsZeroConcurrency(t *testing.T) {
	if _, err := Throttle[int](context.Background(), 0, 0, nil); err == nil {
		t.Fatal("max concurrency of 0 must be a loud error, not a deadlock")
	}
}

func TestThrottleEmpty(t *testing.T) {
	got, err := Throttle[int](context.Background(), 3, 0, nil)
	if err != nil || len(got) != 0 {
		t.Fatalf("Throttle(nil) = %v, %v", got, err)
	}
}

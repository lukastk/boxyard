// Package runner runs boxyard's external commands. In practice that means
// rclone, and everything here exists because of how rclone behaves when the
// machine it is running on misbehaves.
//
// Ported from src/boxyard/_utils/base.py (run_cmd_async, the suspend watchdog
// and async_throttler).
//
// # THREE THINGS THIS PACKAGE GUARANTEES
//
// 1. EVERY SUBPROCESS GETS ITS OWN PROCESS GROUP.
//
// rclone spawns children, and a kill aimed at rclone alone leaves them behind
// holding pipes and sockets. Each command is therefore started with
// SysProcAttr{Setpgid: true} and killed with syscall.Kill(-pgid, SIGKILL), so a
// timeout or a suspend kill takes the whole tree down. This is also what lets
// Wait return: the output pipes stay open as long as any process in the group
// holds them.
//
// 2. IN-FLIGHT SUBPROCESSES ARE KILLED WHEN THE MACHINE WAKES FROM SLEEP.
//
// rclone does not reliably notice that its TCP connections died while the
// machine was suspended. Its own --timeout (5m IO idle), --contimeout (1m) and
// --sftp-idle-timeout (1m) were ALL in effect when two `lsjson` processes,
// spawned two seconds before an idle sleep, span at 100% CPU with no open
// sockets for 9.5 hours. The watchdog detects the wake and kills them, so the
// caller fails loudly and can retry on fresh connections.
//
// Detection compares the WALL clock against the MONOTONIC clock: the wall clock
// advances while the machine is asleep and the monotonic clock does not, so a
// divergence between the two across one poll interval is a suspend. See
// watchdogTick — the comment there matters, because the Go-specific way to get
// this wrong fails silently.
//
// 3. A CALLER CAN TELL "KILLED BY THE WATCHDOG" FROM "EXITED ON ITS OWN".
//
// A suspend kill is not the command's own failure and must not be reported as
// one. Killed pids are recorded, and Run turns them into a *SuspendError.
//
// # TIMEOUT POLICY
//
// The timeout passed to Run is a WALL-CLOCK CEILING and suits only operations
// whose work is inherently bounded — listings and metadata reads. Transfers
// have no meaningful upper bound (a big box legitimately takes hours) and must
// be left unbounded; the suspend watchdog covers them instead.
//
// # PLATFORM
//
// Process groups here are POSIX. boxyard targets macOS, Linux and Android
// (termux); there is no Windows build.
package runner

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/lukastk/boxyard/internal/boxconst"
)

// MaxConcurrentSubprocesses caps how many subprocesses boxyard has in flight at
// once, process-wide, so that a fan-out over 583 boxes cannot exhaust the file
// descriptor table. It is deliberately separate from (and larger than) the
// config's max_concurrent_rclone_ops, which throttles logical sync operations
// rather than raw processes.
const MaxConcurrentSubprocesses = 10

// Suspend detection parameters, from the shared constants.
const (
	suspendPollInterval = time.Duration(boxconst.SuspendPollIntervalSeconds * float64(time.Second))
	suspendThreshold    = time.Duration(boxconst.SuspendDetectThresholdSeconds * float64(time.Second))
)

// Result is the outcome of a command that ran to completion.
//
// A non-zero ExitCode is NOT an error: rclone uses exit codes to report
// expected states (3 = directory not found), and the caller decides what a
// given code means. Run returns an error only when the command could not be
// run, or was killed.
type Result struct {
	ExitCode int
	Stdout   string
	Stderr   string
}

// OK reports whether the command exited zero.
func (r Result) OK() bool { return r.ExitCode == 0 }

// TimeoutError reports that a command exceeded its wall-clock ceiling and was
// killed, along with its whole process group.
type TimeoutError struct {
	Argv    []string
	Timeout time.Duration
}

func (e *TimeoutError) Error() string {
	return fmt.Sprintf("`%s` exceeded its %ss timeout and was killed.",
		commandHead(e.Argv), strconv.FormatFloat(e.Timeout.Seconds(), 'g', -1, 64))
}

// SuspendError reports that a command was killed because the machine resumed
// from sleep while it was running. The command did not fail; its connections
// died underneath it, and the right response is to retry.
type SuspendError struct {
	Argv []string
}

func (e *SuspendError) Error() string {
	return fmt.Sprintf("`%s` was killed because the machine resumed from sleep; "+
		"its connections were dead.", commandHead(e.Argv))
}

// commandHead names a command in an error message using its first two argv
// elements ("rclone lsjson"), which identifies the operation without spilling
// paths into the message.
func commandHead(argv []string) string {
	if len(argv) > 2 {
		argv = argv[:2]
	}
	return strings.Join(argv, " ")
}

// RunFunc is the shape of Run. It is the seam packages take when they need to
// substitute a fake subprocess layer in tests; see internal/rclone.
//
// A timeout of zero or less means "no wall-clock ceiling" — see the package
// doc's timeout policy.
type RunFunc func(ctx context.Context, argv []string, timeout time.Duration) (Result, error)

// Run executes argv and returns its exit code and captured output.
//
// timeout is a wall-clock ceiling; zero or less means unbounded. Use a ceiling
// only for bounded work (listings, metadata reads) — never for transfers.
//
// The returned error is non-nil only when the command did not run to
// completion: a *TimeoutError, a *SuspendError, a context error, or a failure
// to start the process at all (a missing binary, say). A command that ran and
// exited non-zero returns a Result and a nil error.
func Run(ctx context.Context, argv []string, timeout time.Duration) (Result, error) {
	return defaultEngine.run(ctx, argv, timeout)
}

// engine owns the subprocess semaphore, the live-process registry and the
// suspend watchdog.
//
// It exists as a type purely so the watchdog is testable: the package uses one
// global instance, while tests construct their own with fake clocks and drive
// watchdogTick directly, rather than suspending the machine.
type engine struct {
	// sem bounds concurrent subprocesses. Held for the whole lifetime of the
	// command, as the Python's `async with semaphore` is.
	sem chan struct{}

	// mu guards live and suspendKilled. The watchdog holds it while killing, so
	// a process cannot be deregistered midway through a kill sweep.
	mu            sync.Mutex
	live          map[int]struct{}
	suspendKilled map[int]struct{}

	watchdogOnce sync.Once

	// Seams. The real implementations are installed by newEngine; tests replace
	// them. Keeping wall and monotonic time as two SEPARATE functions is
	// deliberate — see watchdogTick.
	wallNow      func() time.Time
	monoNow      func() time.Duration
	sleep        func(time.Duration)
	killGroup    func(pid int) error
	pollInterval time.Duration
	threshold    time.Duration
}

var defaultEngine = newEngine()

func newEngine() *engine {
	return &engine{
		sem:           make(chan struct{}, MaxConcurrentSubprocesses),
		live:          make(map[int]struct{}),
		suspendKilled: make(map[int]struct{}),
		wallNow:       realWallNow,
		monoNow:       realMonoNow,
		sleep:         time.Sleep,
		killGroup:     killProcessGroup,
		pollInterval:  suspendPollInterval,
		threshold:     suspendThreshold,
	}
}

// processStart anchors the monotonic clock. A time.Time returned by time.Now
// carries a monotonic reading, and time.Since uses it, so realMonoNow measures
// elapsed time that does NOT advance while the machine is asleep.
var processStart = time.Now()

// realWallNow returns the current wall-clock time with the monotonic reading
// STRIPPED.
//
// The strip is the entire point. time.Time.Sub prefers the monotonic readings
// when both operands carry one, so subtracting two unstripped time.Now values
// yields elapsed monotonic time — which does not move during a suspend. An
// implementation that forgot Round(0) here would compute a "wall" delta equal
// to the monotonic delta, making the difference in watchdogTick identically
// zero and the watchdog silently dead. See TestRealClockSeams.
func realWallNow() time.Time { return time.Now().Round(0) }

// realMonoNow returns monotonic elapsed time since process start.
func realMonoNow() time.Duration { return time.Since(processStart) }

// ensureWatchdog starts the suspend watchdog on first use, matching the
// Python's lazy _ensure_suspend_watchdog. It runs for the life of the process.
func (e *engine) ensureWatchdog() {
	e.watchdogOnce.Do(func() { go e.watchdogLoop() })
}

func (e *engine) watchdogLoop() {
	for {
		e.watchdogTick()
	}
}

// watchdogTick performs one poll of the suspend watchdog and returns how many
// process groups it killed.
//
// It samples both clocks, sleeps for one poll interval, samples again, and
// treats wall-minus-monotonic elapsed time as time the machine spent asleep.
// The threshold only has to sit above normal clock slew (NTP steps a few
// seconds at most); real sleeps are minutes to hours.
//
// The two clocks come from two SEPARATE seams on purpose. Sampling both from a
// single time.Time source would make this subtraction always zero — see
// realWallNow — and the failure would be invisible: no error, no log, just a
// watchdog that never fires.
func (e *engine) watchdogTick() int {
	wallBefore, monoBefore := e.wallNow(), e.monoNow()
	e.sleep(e.pollInterval)
	sleptFor := e.wallNow().Sub(wallBefore) - (e.monoNow() - monoBefore)

	if sleptFor < e.threshold {
		return 0
	}
	return e.killLive()
}

// killLive kills every in-flight process group and records the pids, so each
// waiting Run can report a *SuspendError rather than a command failure.
//
// The kills happen under mu, which run also takes to deregister a finished
// process, so a pid cannot be forgotten part-way through the sweep. (A process
// reaped in the instant before its deregistration could in principle have its
// pid reused by then; that needs the whole pid space to wrap inside that
// window, and the Python implementation carries the same exposure.)
func (e *engine) killLive() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	for pid := range e.live {
		e.suspendKilled[pid] = struct{}{}
		// A kill that fails means the process is already gone; the pid stays
		// recorded so its Run still reports the suspend rather than a bogus
		// exit status.
		_ = e.killGroup(pid)
	}
	return len(e.live)
}

func (e *engine) register(pid int) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.live[pid] = struct{}{}
}

// deregister removes pid from the live set and reports whether the watchdog
// killed it.
func (e *engine) deregister(pid int) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	delete(e.live, pid)
	_, killed := e.suspendKilled[pid]
	delete(e.suspendKilled, pid)
	return killed
}

func (e *engine) run(ctx context.Context, argv []string, timeout time.Duration) (Result, error) {
	if len(argv) == 0 {
		return Result{}, errors.New("runner: empty command")
	}
	e.ensureWatchdog()

	select {
	case e.sem <- struct{}{}:
	case <-ctx.Done():
		return Result{}, fmt.Errorf("`%s` was cancelled before it started: %w", commandHead(argv), ctx.Err())
	}
	defer func() { <-e.sem }()

	var stdout, stderr bytes.Buffer
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	// Own process group, so a kill takes this command's children down with it.
	// Note this is NOT exec.CommandContext: that would signal the process
	// alone, leaving the group behind.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		return Result{}, fmt.Errorf("could not run `%s`: %w", commandHead(argv), err)
	}
	pid := cmd.Process.Pid
	e.register(pid)

	waited := make(chan error, 1)
	go func() { waited <- cmd.Wait() }()

	var timeoutC <-chan time.Time
	if timeout > 0 {
		timer := time.NewTimer(timeout)
		defer timer.Stop()
		timeoutC = timer.C
	}

	select {
	case err := <-waited:
		suspendKilled := e.deregister(pid)
		if suspendKilled {
			return Result{}, &SuspendError{Argv: argv}
		}
		code, codeErr := exitCode(err)
		if codeErr != nil {
			return Result{}, fmt.Errorf("`%s` failed: %w", commandHead(argv), codeErr)
		}
		return Result{ExitCode: code, Stdout: stdout.String(), Stderr: stderr.String()}, nil

	case <-timeoutC:
		_ = e.killGroup(pid)
		<-waited
		e.deregister(pid)
		return Result{}, &TimeoutError{Argv: argv, Timeout: timeout}

	case <-ctx.Done():
		_ = e.killGroup(pid)
		<-waited
		e.deregister(pid)
		return Result{}, fmt.Errorf("`%s` was cancelled and killed: %w", commandHead(argv), ctx.Err())
	}
}

// exitCode extracts the exit status from the error cmd.Wait returned. An
// *exec.ExitError is an ordinary outcome; anything else (an I/O failure on the
// output pipes, a process killed by a signal we did not send) is a real error
// and is returned as one rather than being flattened into a status code.
func exitCode(waitErr error) (int, error) {
	if waitErr == nil {
		return 0, nil
	}
	var ee *exec.ExitError
	if !errors.As(waitErr, &ee) {
		return 0, waitErr
	}
	status, ok := ee.Sys().(syscall.WaitStatus)
	if ok && status.Signaled() {
		return 0, fmt.Errorf("killed by signal %s", status.Signal())
	}
	return ee.ExitCode(), nil
}

// killProcessGroup kills pid and everything it spawned.
//
// It falls back to killing the process alone if the group cannot be resolved or
// signalled — the process may already have been reaped, or (in principle) not
// be in a group of ours. This mirrors the Python's ProcessLookupError /
// PermissionError handling.
func killProcessGroup(pid int) error {
	if pgid, err := syscall.Getpgid(pid); err == nil {
		// SUICIDE GUARD. If the child somehow shares OUR process group, then
		// kill(-pgid, SIGKILL) would kill boxyard itself — and, when boxyard is
		// run from a shell, everything else in that group. That is exactly what
		// happens if the Setpgid above is ever dropped, and the symptom (the
		// whole process tree vanishing with no output) gives no hint of the
		// cause. Kill only the child in that case, and say so.
		if self, selfErr := syscall.Getpgid(0); selfErr == nil && pgid == self {
			killErr := syscall.Kill(pid, syscall.SIGKILL)
			return fmt.Errorf("pid %d was not placed in its own process group "+
				"(shares group %d with boxyard); killed the process alone to avoid "+
				"killing ourselves: %v", pid, pgid, killErr)
		}
		if err := syscall.Kill(-pgid, syscall.SIGKILL); err == nil {
			return nil
		}
	}
	return syscall.Kill(pid, syscall.SIGKILL)
}

// Throttle runs tasks with bounded concurrency and returns their results in the
// order the tasks were given.
//
// Ported from the Python async_throttler. Every task is run to completion even
// if an earlier one fails — nothing is cancelled on the first error — and then
// the FIRST error in task order is returned. That ordering is deliberate: with
// a fan-out over many boxes, the error the operator sees should be stable
// rather than a race between workers.
//
// timeout, if positive, bounds each individual task via its context. Zero
// leaves tasks unbounded.
func Throttle[T any](
	ctx context.Context,
	maxConcurrency int,
	timeout time.Duration,
	tasks []func(context.Context) (T, error),
) ([]T, error) {
	if maxConcurrency < 1 {
		return nil, fmt.Errorf("runner: max concurrency must be at least 1, got %d", maxConcurrency)
	}

	results := make([]T, len(tasks))
	errs := make([]error, len(tasks))
	sem := make(chan struct{}, maxConcurrency)

	var wg sync.WaitGroup
	for i, task := range tasks {
		wg.Add(1)
		go func() {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()

			taskCtx := ctx
			if timeout > 0 {
				var cancel context.CancelFunc
				taskCtx, cancel = context.WithTimeout(ctx, timeout)
				defer cancel()
			}
			results[i], errs[i] = task(taskCtx)
		}()
	}
	wg.Wait()

	for _, err := range errs {
		if err != nil {
			return nil, err
		}
	}
	return results, nil
}

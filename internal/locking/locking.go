// Package locking provides the advisory file locks that coordinate concurrent
// boxyard operations on a single machine.
//
// Several boxyard processes routinely run at the same time: the supervisor sync
// loop, interactive CLI commands, and multi-sync's parallel workers. They
// serialise against each other through lock files under the boxyard data
// directory:
//
//	<boxyard_data_path>/locks/
//	    global.lock                    # protects boxyard_meta.json
//	    boxes/{index_name}/sync.lock   # per-box sync/include/exclude/delete
//
// Locks are POSIX advisory locks (flock(2)) held on an open file descriptor, so
// they are released automatically if the holding process dies. The lock *files*
// outlive the lock and are pruned by CleanupStaleLocks.
//
// # Failure policy
//
// A lock that cannot be taken is always a loud, typed error (*AcquisitionError).
// Nothing in this package ever falls back to "carry on unlocked": callers must
// see the failure and abort the operation.
package locking

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/gofrs/flock"
)

// Timeouts and intervals, ported from src/boxyard/_utils/locking.py.
const (
	// GlobalLockTimeout bounds the wait for the global lock. The global lock is
	// only ever held across a boxyard_meta.json read/modify/write, so a wait
	// longer than this means something is wedged.
	GlobalLockTimeout = 30 * time.Second

	// BoxSyncLockTimeout bounds the wait for a per-box sync lock. It is long
	// because the holder may legitimately be running a full rclone transfer.
	BoxSyncLockTimeout = 10 * time.Minute

	// LockPollInterval is the delay between non-blocking acquisition attempts.
	// Acquisition polls rather than blocking in the kernel so that a caller's
	// context.Context can cancel a pending wait.
	LockPollInterval = 100 * time.Millisecond
)

// Stale-lock age thresholds, ported from the Python defaults.
const (
	// DefaultStaleLockMaxAge is the threshold used by an explicit cleanup.
	DefaultStaleLockMaxAge = 24 * time.Hour

	// DefaultAutoCleanupMaxAge is the threshold used by the startup
	// auto-cleanup. It is shorter than DefaultStaleLockMaxAge, which is safe
	// because removal is always gated on the lock being unheld.
	DefaultAutoCleanupMaxAge = time.Hour
)

// locksDirName is the subdirectory of the boxyard data directory that holds
// every lock file.
const locksDirName = "locks"

// AcquisitionError reports that a lock could not be taken.
//
// It carries the lock's human-readable kind, its path on disk and the timeout
// that elapsed, so the message tells the operator exactly which file to look at
// and what is likely holding it.
type AcquisitionError struct {
	// LockType names the lock, e.g. "global" or `box sync (20251122_143022_a7kx9__foo)`.
	LockType string
	// LockPath is the lock file on disk.
	LockPath string
	// Timeout is how long the caller was willing to wait.
	Timeout time.Duration
	// Hint explains, in prose, what is probably holding the lock.
	Hint string
	// Err is context.DeadlineExceeded for a plain timeout, context.Canceled if
	// the caller's context was cancelled, or the underlying failure (mkdir,
	// open, flock) otherwise. It is never nil.
	Err error
}

func (e *AcquisitionError) Error() string {
	var b strings.Builder
	fmt.Fprintf(&b, "could not acquire %s lock", e.LockType)
	switch {
	case errors.Is(e.Err, context.DeadlineExceeded):
		fmt.Fprintf(&b, " within %s: %s", e.Timeout, e.Hint)
	case errors.Is(e.Err, context.Canceled):
		fmt.Fprintf(&b, ": cancelled after waiting up to %s", e.Timeout)
	default:
		fmt.Fprintf(&b, ": %s", e.Err)
	}
	fmt.Fprintf(&b, " (lock file: %s)", e.LockPath)
	return b.String()
}

// Unwrap exposes the underlying cause so callers can use
// errors.Is(err, context.DeadlineExceeded) to distinguish a timeout from an I/O
// failure.
func (e *AcquisitionError) Unwrap() error { return e.Err }

// TimedOut reports whether the acquisition failed because the timeout elapsed,
// as opposed to an I/O failure or a cancelled context.
func (e *AcquisitionError) TimedOut() bool {
	return errors.Is(e.Err, context.DeadlineExceeded)
}

// Manager owns the lock files for one boxyard data directory.
//
// A Manager holds no state of its own; it is safe for concurrent use, and two
// Managers over the same data directory contend exactly as two processes would.
type Manager struct {
	dataPath  string
	locksPath string

	// ReleaseErrorHandler is called if releasing a lock fails. Releasing
	// happens in a deferred func() that has nowhere to return an error to, but
	// a failed release is never swallowed: if this is nil the error is written
	// to os.Stderr. Set it before taking any lock.
	ReleaseErrorHandler func(error)
}

// NewManager returns a Manager for the given boxyard data directory (the
// directory that also holds boxyard_meta.json). Nothing is created on disk
// until a lock is taken.
func NewManager(boxyardDataPath string) *Manager {
	return &Manager{
		dataPath:  boxyardDataPath,
		locksPath: filepath.Join(boxyardDataPath, locksDirName),
	}
}

// DataPath returns the boxyard data directory this Manager locks within.
func (m *Manager) DataPath() string { return m.dataPath }

// LocksPath returns the directory holding every lock file.
func (m *Manager) LocksPath() string { return m.locksPath }

// GlobalLockPath returns the path of the global lock file.
func (m *Manager) GlobalLockPath() string {
	return filepath.Join(m.locksPath, "global.lock")
}

// BoxSyncLockPath returns the path of a box's sync lock file.
//
// It errors on an index name that is not a single path component. The Python
// original does no validation and would happily create nested directories (or
// escape the locks directory) for a name containing "/" or ".."; refusing is
// both louder and safer, and index names are a single path component by
// construction.
func (m *Manager) BoxSyncLockPath(indexName string) (string, error) {
	if err := validateIndexName(indexName); err != nil {
		return "", err
	}
	return filepath.Join(m.locksPath, "boxes", indexName, "sync.lock"), nil
}

// validateIndexName rejects box index names that are not a single, ordinary
// path component.
func validateIndexName(indexName string) error {
	switch {
	case indexName == "":
		return fmt.Errorf("locking: box index name is empty")
	case indexName == "." || indexName == "..":
		return fmt.Errorf("locking: box index name %q is not a valid path component", indexName)
	case strings.ContainsRune(indexName, '/'):
		return fmt.Errorf("locking: box index name %q must be a single path component (contains %q)", indexName, "/")
	case strings.ContainsRune(indexName, 0):
		return fmt.Errorf("locking: box index name %q contains a NUL byte", indexName)
	case filepath.Base(indexName) != indexName:
		return fmt.Errorf("locking: box index name %q must be a single path component", indexName)
	}
	return nil
}

// GlobalLock acquires the global lock, which protects boxyard_meta.json and any
// other operation on global state.
//
// It returns a release func the caller must defer. See BoxSyncLock for the
// release contract.
func (m *Manager) GlobalLock(timeout time.Duration) (release func(), err error) {
	return m.GlobalLockContext(context.Background(), timeout)
}

// GlobalLockContext is GlobalLock with a caller-supplied context. Cancelling
// ctx aborts a pending acquisition; it has no effect once the lock is held.
func (m *Manager) GlobalLockContext(ctx context.Context, timeout time.Duration) (release func(), err error) {
	lockPath := m.GlobalLockPath()
	fl, err := m.acquire(ctx, lockPath, "global", "another boxyard operation may be in progress", timeout)
	if err != nil {
		return nil, err
	}
	return m.releaseFunc([]unlocker{fl}), nil
}

// BoxSyncLock acquires a box's sync lock, which serialises sync, include,
// exclude and delete operations on that box.
//
// The returned release func must be called exactly once, normally via defer.
// Calling it more than once is harmless (subsequent calls are no-ops). It is
// only ever nil when err is non-nil, so the idiom is:
//
//	release, err := mgr.BoxSyncLock(name, locking.BoxSyncLockTimeout)
//	if err != nil {
//	    return err
//	}
//	defer release()
func (m *Manager) BoxSyncLock(indexName string, timeout time.Duration) (release func(), err error) {
	return m.BoxSyncLockContext(context.Background(), indexName, timeout)
}

// BoxSyncLockContext is BoxSyncLock with a caller-supplied context.
func (m *Manager) BoxSyncLockContext(ctx context.Context, indexName string, timeout time.Duration) (release func(), err error) {
	lockPath, err := m.BoxSyncLockPath(indexName)
	if err != nil {
		return nil, err
	}
	fl, err := m.acquire(
		ctx,
		lockPath,
		fmt.Sprintf("box sync (%s)", indexName),
		fmt.Sprintf("another sync, include, exclude, or delete operation may be in progress on box %q", indexName),
		timeout,
	)
	if err != nil {
		return nil, err
	}
	return m.releaseFunc([]unlocker{fl}), nil
}

// MultipleBoxSyncLocks acquires the sync locks of several boxes at once.
//
// The names are de-duplicated and sorted before any lock is taken. That
// ordering is the deadlock-avoidance guarantee: every caller in every process
// walks the same global order, so two callers asking for overlapping sets can
// never each hold a lock the other is waiting for. Do not "optimise" it away.
//
// timeout applies to each lock individually, matching the Python original.
//
// If any lock cannot be taken, the locks already acquired are released in
// reverse order and the error is returned; the caller ends up holding nothing.
// An empty name list is not an error and yields a no-op release func.
func (m *Manager) MultipleBoxSyncLocks(indexNames []string, timeout time.Duration) (release func(), err error) {
	return m.MultipleBoxSyncLocksContext(context.Background(), indexNames, timeout)
}

// MultipleBoxSyncLocksContext is MultipleBoxSyncLocks with a caller-supplied
// context.
func (m *Manager) MultipleBoxSyncLocksContext(ctx context.Context, indexNames []string, timeout time.Duration) (release func(), err error) {
	names, err := normalizeIndexNames(indexNames)
	if err != nil {
		return nil, err
	}

	acquired := make([]unlocker, 0, len(names))
	for _, name := range names {
		lockPath, err := m.BoxSyncLockPath(name)
		if err != nil {
			releaseAll(acquired, m.reportReleaseError)
			return nil, err
		}
		fl, err := m.acquire(
			ctx,
			lockPath,
			fmt.Sprintf("box sync (%s)", name),
			fmt.Sprintf("another operation may be in progress on box %q", name),
			timeout,
		)
		if err != nil {
			// Unwind in reverse acquisition order.
			releaseAll(acquired, m.reportReleaseError)
			return nil, err
		}
		acquired = append(acquired, fl)
	}
	return m.releaseFunc(acquired), nil
}

// normalizeIndexNames validates, de-duplicates and sorts box index names.
// Sorting is what makes MultipleBoxSyncLocks deadlock-free; de-duplication is
// what stops a repeated name from blocking on itself.
func normalizeIndexNames(indexNames []string) ([]string, error) {
	names := make([]string, 0, len(indexNames))
	for _, name := range indexNames {
		if err := validateIndexName(name); err != nil {
			return nil, err
		}
		names = append(names, name)
	}
	slices.Sort(names)
	return slices.Compact(names), nil
}

// acquire takes one lock, creating its directory on demand.
//
// Acquisition polls at LockPollInterval rather than blocking in the kernel, so
// a cancelled context takes effect promptly and no lock is left half-taken.
// A timeout of zero or less means a single non-blocking attempt.
func (m *Manager) acquire(ctx context.Context, lockPath, lockType, hint string, timeout time.Duration) (*flock.Flock, error) {
	fail := func(cause error) error {
		return &AcquisitionError{
			LockType: lockType,
			LockPath: lockPath,
			Timeout:  timeout,
			Hint:     hint,
			Err:      cause,
		}
	}

	if err := os.MkdirAll(filepath.Dir(lockPath), 0o755); err != nil {
		return nil, fail(fmt.Errorf("cannot create lock directory: %w", err))
	}

	fl := flock.New(lockPath)

	var (
		ok  bool
		err error
	)
	if timeout <= 0 {
		ok, err = fl.TryLock()
		if err == nil && !ok {
			err = context.DeadlineExceeded
		}
	} else {
		waitCtx, cancel := context.WithTimeout(ctx, timeout)
		defer cancel()
		ok, err = fl.TryLockContext(waitCtx, LockPollInterval)
	}

	if err != nil {
		return nil, fail(err)
	}
	if !ok {
		// TryLockContext only returns (false, nil) if the context ended, but be
		// explicit rather than handing back a lock we do not hold.
		return nil, fail(context.DeadlineExceeded)
	}
	return fl, nil
}

// unlocker is the part of *flock.Flock that the release path actually needs.
// Narrowing to an interface here also lets the release path be tested for
// ordering and error reporting without contriving a real flock failure.
type unlocker interface {
	Unlock() error
	Path() string
}

// releaseFunc builds the release func handed back to callers. It is idempotent:
// the locks are released on the first call and later calls do nothing.
func (m *Manager) releaseFunc(locks []unlocker) func() {
	var once sync.Once
	return func() {
		once.Do(func() {
			releaseAll(locks, m.reportReleaseError)
		})
	}
}

// releaseAll releases locks in reverse acquisition order, reporting — never
// swallowing — any failure. It returns the paths in the order they were
// released.
func releaseAll(locks []unlocker, report func(error)) []string {
	released := make([]string, 0, len(locks))
	for i := len(locks) - 1; i >= 0; i-- {
		lock := locks[i]
		if err := lock.Unlock(); err != nil {
			report(fmt.Errorf("locking: failed to release lock %s: %w", lock.Path(), err))
			continue
		}
		released = append(released, lock.Path())
	}
	return released
}

// reportReleaseError surfaces a failed release. A release failure cannot be
// returned (the caller is inside a defer), but it must not be silent: it means
// a lock may still be held and the next operation on that box will stall.
func (m *Manager) reportReleaseError(err error) {
	if m.ReleaseErrorHandler != nil {
		m.ReleaseErrorHandler(err)
		return
	}
	fmt.Fprintf(os.Stderr, "boxyard: %v\n", err)
}

// CleanupStaleLocks removes lock files older than maxAge that nobody currently
// holds.
//
// The "nobody holds it" test is what makes this safe: a long-running sync may
// legitimately have held its lock for hours, and its file is left alone. Only
// leftovers from crashed processes are removed.
//
// It returns the paths removed. A missing locks directory is not an error.
// Unlike the Python original, which swallowed every OSError, only "file
// disappeared underneath us" is tolerated (another process cleaning up
// concurrently is expected); anything else is returned.
func CleanupStaleLocks(boxyardDataPath string, maxAge time.Duration) ([]string, error) {
	if maxAge < 0 {
		return nil, fmt.Errorf("locking: stale lock max age must not be negative, got %s", maxAge)
	}

	locksPath := filepath.Join(boxyardDataPath, locksDirName)
	info, err := os.Stat(locksPath)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("locking: cannot stat lock directory %s: %w", locksPath, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("locking: lock path %s is not a directory", locksPath)
	}

	cutoff := time.Now().Add(-maxAge)
	var removed []string

	walkErr := filepath.WalkDir(locksPath, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				return nil // removed by a concurrent cleanup
			}
			return fmt.Errorf("locking: cannot walk %s: %w", path, err)
		}
		if d.IsDir() || !strings.HasSuffix(d.Name(), ".lock") {
			return nil
		}

		fi, err := d.Info()
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				return nil
			}
			return fmt.Errorf("locking: cannot stat lock file %s: %w", path, err)
		}
		if !fi.ModTime().Before(cutoff) {
			return nil
		}

		fl := flock.New(path)
		held, err := fl.TryLock()
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				return nil
			}
			return fmt.Errorf("locking: cannot probe lock file %s: %w", path, err)
		}
		if !held {
			// A live operation owns this lock. Leave it alone.
			return nil
		}

		// Remove while still holding the lock, so no one can acquire it in the
		// window between the probe and the unlink. (A process already blocked
		// on this file can still end up holding a lock on the unlinked inode —
		// that race is inherent to flock+unlink and is why removal is gated on
		// age as well.)
		rmErr := os.Remove(path)
		unlockErr := fl.Unlock()

		if rmErr != nil && !errors.Is(rmErr, fs.ErrNotExist) {
			return fmt.Errorf("locking: cannot remove stale lock file %s: %w", path, rmErr)
		}
		if unlockErr != nil {
			return fmt.Errorf("locking: failed to release probe lock on %s: %w", path, unlockErr)
		}
		if rmErr == nil {
			removed = append(removed, path)
		}
		return nil
	})
	if walkErr != nil {
		return removed, walkErr
	}
	return removed, nil
}

// AutoCleanupStaleLocks is the startup cleanup: CleanupStaleLocks plus an
// optional report of what it removed.
//
// The Python original took a verbose bool and printed to stdout; taking an
// io.Writer instead lets the caller decide where the report goes. A nil writer
// means no report.
func AutoCleanupStaleLocks(boxyardDataPath string, maxAge time.Duration, report io.Writer) ([]string, error) {
	removed, err := CleanupStaleLocks(boxyardDataPath, maxAge)
	if err != nil {
		return removed, err
	}
	if report == nil || len(removed) == 0 {
		return removed, nil
	}
	if _, err := fmt.Fprintf(report, "Cleaned up %d stale lock file(s):\n", len(removed)); err != nil {
		return removed, fmt.Errorf("locking: cannot write cleanup report: %w", err)
	}
	for _, path := range removed {
		if _, err := fmt.Fprintf(report, "  - %s\n", path); err != nil {
			return removed, fmt.Errorf("locking: cannot write cleanup report: %w", err)
		}
	}
	return removed, nil
}

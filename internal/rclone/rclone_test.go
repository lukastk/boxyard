package rclone

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/runner"
)

// This file ports src/tests/unit/_utils/test_rclone_cmd_builder.py (57 tests)
// and src/tests/unit/_utils/test_rclone_resolution.py (8 tests).
//
// The Python asserts on the shell-quoted string that return_command=True hands
// back ("--include '*.py'" in result). Asserting on the argv SLICE instead is
// not a translation convenience: the quoting the Python inspects is an artefact
// of shlex.join, and the property that actually matters — that a pattern or a
// path with a space stays ONE argument — is invisible in a joined string and
// checked directly here.
//
// SAFETY: nothing in this file touches the user's boxyard, and no test reaches
// a network remote. The integration tests at the bottom run the real rclone
// binary against local paths in t.TempDir() only.

const testConfig = "/tmp/rclone.conf"

// newTestClient returns a client whose binary is a fixed fake path, so argv
// assertions do not depend on where rclone is installed.
func newTestClient() *Client { return NewWithBinary("/usr/bin/rclone", testConfig) }

// fakeRun returns a RunFunc that records the argv it was called with and
// returns a canned result.
func fakeRun(res runner.Result, calls *[][]string) runner.RunFunc {
	return func(_ context.Context, argv []string, _ time.Duration) (runner.Result, error) {
		*calls = append(*calls, slices.Clone(argv))
		return res, nil
	}
}

// clientReturning is a client whose subprocess layer always returns the given
// exit code and streams.
func clientReturning(exitCode int, stdout, stderr string) (*Client, *[][]string) {
	calls := &[][]string{}
	c := newTestClient()
	c.Exec = fakeRun(runner.Result{ExitCode: exitCode, Stdout: stdout, Stderr: stderr}, calls)
	return c, calls
}

// ---------------------------------------------------------------------------
// argv assertion helpers
// ---------------------------------------------------------------------------

func hasFlag(argv []string, flag string) bool { return slices.Contains(argv, flag) }

// hasFlagValue reports whether flag appears immediately followed by value.
// Adjacency is the point: rclone reads these as pairs.
func hasFlagValue(argv []string, flag, value string) bool {
	for i := 0; i < len(argv)-1; i++ {
		if argv[i] == flag && argv[i+1] == value {
			return true
		}
	}
	return false
}

func assertFlag(t *testing.T, argv []string, flag string) {
	t.Helper()
	if !hasFlag(argv, flag) {
		t.Errorf("argv is missing %s: %q", flag, argv)
	}
}

func assertNoFlag(t *testing.T, argv []string, flag string) {
	t.Helper()
	if hasFlag(argv, flag) {
		t.Errorf("argv should not contain %s: %q", flag, argv)
	}
}

func assertFlagValue(t *testing.T, argv []string, flag, value string) {
	t.Helper()
	if !hasFlagValue(argv, flag, value) {
		t.Errorf("argv is missing %s %q: %q", flag, value, argv)
	}
}

func assertArg(t *testing.T, argv []string, arg string) {
	t.Helper()
	if !slices.Contains(argv, arg) {
		t.Errorf("argv is missing the argument %q: %q", arg, argv)
	}
}

func assertSubcommand(t *testing.T, argv []string, sub string) {
	t.Helper()
	if len(argv) < 2 {
		t.Fatalf("argv too short: %q", argv)
	}
	if filepath.Base(argv[0]) != "rclone" {
		t.Errorf("argv[0] = %q, want a path ending in rclone", argv[0])
	}
	if argv[1] != sub {
		t.Errorf("argv[1] = %q, want %q", argv[1], sub)
	}
}

// ---------------------------------------------------------------------------
// BisyncResult (TestBisyncResult: 2 tests)
// ---------------------------------------------------------------------------

func TestBisyncResultValues(t *testing.T) {
	cases := map[BisyncResult]string{
		BisyncSuccess:         "success",
		BisyncConflicts:       "conflicts",
		BisyncNeedsResync:     "needs_resync",
		BisyncAllFilesChanged: "all_files_changed",
		BisyncOtherError:      "other_error",
	}
	for result, want := range cases {
		if string(result) != want {
			t.Errorf("%v = %q, want %q", result, string(result), want)
		}
	}
}

func TestBisyncResultCount(t *testing.T) {
	if len(AllBisyncResults) != 5 {
		t.Fatalf("AllBisyncResults has %d entries, want 5", len(AllBisyncResults))
	}
	seen := map[BisyncResult]bool{}
	for _, r := range AllBisyncResults {
		if seen[r] {
			t.Fatalf("duplicate entry %q in AllBisyncResults", r)
		}
		seen[r] = true
	}
}

// ---------------------------------------------------------------------------
// copy argv (TestRcloneCopyCommand: 10 tests)
// ---------------------------------------------------------------------------

func TestCopyArgsBasic(t *testing.T) {
	argv := newTestClient().CopyArgs(
		Remote("myremote", "bucket/data"), Local("/local/path"), TransferOptions{})

	assertSubcommand(t, argv, "copy")
	assertFlagValue(t, argv, "--config", testConfig)
	assertArg(t, argv, "myremote:bucket/data")
	assertArg(t, argv, "/local/path")
	assertFlag(t, argv, "--links")
	assertFlag(t, argv, "--fast-list")
}

func TestCopyArgsDryRun(t *testing.T) {
	argv := newTestClient().CopyArgs(
		Remote("remote", "path"), Local("/dest"), TransferOptions{DryRun: true})
	assertFlag(t, argv, "--dry-run")
}

func TestCopyArgsProgress(t *testing.T) {
	argv := newTestClient().CopyArgs(
		Remote("remote", "path"), Local("/dest"), TransferOptions{Progress: true})
	assertFlag(t, argv, "--progress")
}

func TestCopyArgsIncludePatterns(t *testing.T) {
	argv := newTestClient().CopyArgs(
		Remote("remote", "path"), Local("/dest"),
		TransferOptions{Include: []string{"*.py", "*.txt"}})
	assertFlagValue(t, argv, "--include", "*.py")
	assertFlagValue(t, argv, "--include", "*.txt")
}

func TestCopyArgsExcludePatterns(t *testing.T) {
	argv := newTestClient().CopyArgs(
		Remote("remote", "path"), Local("/dest"),
		TransferOptions{Exclude: []string{".git/", "node_modules/"}})
	assertFlagValue(t, argv, "--exclude", ".git/")
	assertFlagValue(t, argv, "--exclude", "node_modules/")
}

func TestCopyArgsFilterPatterns(t *testing.T) {
	argv := newTestClient().CopyArgs(
		Remote("remote", "path"), Local("/dest"),
		TransferOptions{Filter: []string{"+ *.py", "- *"}})
	// Each filter rule is one argv element, spaces and all. Joined into a shell
	// string it would need quoting; as argv it needs nothing.
	assertFlagValue(t, argv, "--filter", "+ *.py")
	assertFlagValue(t, argv, "--filter", "- *")
}

func TestCopyArgsIncludeFile(t *testing.T) {
	argv := newTestClient().CopyArgs(
		Remote("remote", "path"), Local("/dest"),
		TransferOptions{IncludeFile: "/tmp/include.txt"})
	assertFlagValue(t, argv, "--include-from", "/tmp/include.txt")
}

func TestCopyArgsExcludeFile(t *testing.T) {
	argv := newTestClient().CopyArgs(
		Remote("remote", "path"), Local("/dest"),
		TransferOptions{ExcludeFile: "/tmp/exclude.txt"})
	assertFlagValue(t, argv, "--exclude-from", "/tmp/exclude.txt")
}

func TestCopyArgsFiltersFile(t *testing.T) {
	argv := newTestClient().CopyArgs(
		Remote("remote", "path"), Local("/dest"),
		TransferOptions{FiltersFile: "/tmp/filters.txt"})
	assertFlagValue(t, argv, "--filters-file", "/tmp/filters.txt")
}

func TestCopyArgsLocalToLocal(t *testing.T) {
	argv := newTestClient().CopyArgs(Local("/local/source"), Local("/local/dest"), TransferOptions{})
	assertArg(t, argv, "/local/source")
	assertArg(t, argv, "/local/dest")
	for _, a := range argv {
		if strings.Contains(a, ":/local") {
			t.Errorf("a local path must not be given a remote prefix: %q", a)
		}
	}
}

// TestArgsKeepSpacesInOneArgument covers the two real boxes whose names contain
// spaces. This is the property the Python's shlex.join assertions cannot see.
func TestArgsKeepSpacesInOneArgument(t *testing.T) {
	box := "20260101_ab1cd__my box with spaces"
	src := Remote("hetzner-box", "boxyard/boxes/"+box+"/data")
	dst := Local("/home/u/dev/" + box + "/data")

	argv := newTestClient().CopyArgs(src, dst, TransferOptions{})
	assertArg(t, argv, "hetzner-box:boxyard/boxes/"+box+"/data")
	assertArg(t, argv, "/home/u/dev/"+box+"/data")

	// And nothing shell-ish leaks in: no argument is a fragment of a name.
	for _, a := range argv {
		if a == "my" || a == "box" {
			t.Fatalf("a box name was split across arguments: %q", argv)
		}
	}
}

// ---------------------------------------------------------------------------
// copyto argv (TestRcloneCopytoCommand: 2 tests)
// ---------------------------------------------------------------------------

func TestCopytoArgsBasic(t *testing.T) {
	argv := newTestClient().CopytoArgs(
		Local("/local/file.txt"), Remote("remote", "bucket/file.txt"), CopytoOptions{})

	assertSubcommand(t, argv, "copyto")
	assertFlagValue(t, argv, "--config", testConfig)
	assertArg(t, argv, "/local/file.txt")
	assertArg(t, argv, "remote:bucket/file.txt")
	// copyto names one object exactly; a recursive listing optimisation and
	// link handling do not apply.
	assertNoFlag(t, argv, "--fast-list")
	assertNoFlag(t, argv, "--links")
}

func TestCopytoArgsProgress(t *testing.T) {
	argv := newTestClient().CopytoArgs(Local("/source"), Local("/dest"), CopytoOptions{Progress: true})
	assertFlag(t, argv, "--progress")
}

// TestCopytoArgsDryRunIsHonoured covers a Python bug rather than Python
// behaviour: rclone_copyto takes a dry_run parameter and never emits
// --dry-run, so asking for a dry run would silently write. See PARITY-NOTES.
func TestCopytoArgsDryRunIsHonoured(t *testing.T) {
	argv := newTestClient().CopytoArgs(Local("/source"), Local("/dest"), CopytoOptions{DryRun: true})
	assertFlag(t, argv, "--dry-run")
}

// ---------------------------------------------------------------------------
// sync argv (TestRcloneSyncCommand: 3 tests)
// ---------------------------------------------------------------------------

func TestSyncArgsBasic(t *testing.T) {
	argv := newTestClient().SyncArgs(Local("/local/path"), Remote("remote", "bucket/backup"), SyncOptions{})

	assertSubcommand(t, argv, "sync")
	assertFlagValue(t, argv, "--config", testConfig)
	assertArg(t, argv, "/local/path")
	assertArg(t, argv, "remote:bucket/backup")
}

func TestSyncArgsBackupDir(t *testing.T) {
	argv := newTestClient().SyncArgs(Local("/source"), Local("/dest"),
		SyncOptions{BackupPath: "/backup/dir"})
	assertFlagValue(t, argv, "--backup-dir", "/backup/dir")
}

func TestSyncArgsAllOptions(t *testing.T) {
	argv := newTestClient().SyncArgs(
		Remote("src_remote", "data"), Remote("dst_remote", "backup"),
		SyncOptions{
			TransferOptions: TransferOptions{
				Include:     []string{"*.txt"},
				Exclude:     []string{"*.tmp"},
				Filter:      []string{"+ important/"},
				IncludeFile: "/inc.txt",
				ExcludeFile: "/exc.txt",
				FiltersFile: "/filters.txt",
				DryRun:      true,
				Progress:    true,
			},
			BackupPath: "/backup",
		})

	assertFlagValue(t, argv, "--include", "*.txt")
	assertFlagValue(t, argv, "--exclude", "*.tmp")
	assertFlagValue(t, argv, "--filter", "+ important/")
	assertFlagValue(t, argv, "--include-from", "/inc.txt")
	assertFlagValue(t, argv, "--exclude-from", "/exc.txt")
	assertFlagValue(t, argv, "--filters-file", "/filters.txt")
	assertFlagValue(t, argv, "--backup-dir", "/backup")
	assertFlag(t, argv, "--dry-run")
	assertFlag(t, argv, "--progress")
}

// TestTransferArgsFilterOrder pins the argv order the Python builds. rclone
// applies filter rules in the order given, so reordering them changes which
// files move.
func TestTransferArgsFilterOrder(t *testing.T) {
	argv := newTestClient().CopyArgs(Local("/a"), Local("/b"), TransferOptions{
		Include:     []string{"inc"},
		IncludeFile: "/incf",
		Exclude:     []string{"exc"},
		ExcludeFile: "/excf",
		Filter:      []string{"filt"},
		FiltersFile: "/filtf",
		DryRun:      true,
		Progress:    true,
	})

	want := []string{
		"/usr/bin/rclone", "copy", "--config", testConfig, "--links", "/a", "/b",
		"--dry-run", "--fast-list",
		"--include", "inc", "--include-from", "/incf",
		"--exclude", "exc", "--exclude-from", "/excf",
		"--filter", "filt", "--filters-file", "/filtf",
		"--progress",
	}
	if !slices.Equal(argv, want) {
		t.Fatalf("argv mismatch\n got: %q\nwant: %q", argv, want)
	}
}

// ---------------------------------------------------------------------------
// bisync argv (TestRcloneBisyncCommand: 3 tests)
// ---------------------------------------------------------------------------

func TestBisyncArgsBasic(t *testing.T) {
	argv := newTestClient().BisyncArgs(Local("/local"), Remote("remote", "bucket"), BisyncOptions{})
	assertSubcommand(t, argv, "bisync")
	assertFlagValue(t, argv, "--config", testConfig)
	assertNoFlag(t, argv, "--resync")
	assertNoFlag(t, argv, "--force")
}

func TestBisyncArgsResync(t *testing.T) {
	argv := newTestClient().BisyncArgs(Local("/local"), Remote("remote", "bucket"),
		BisyncOptions{Resync: true})
	assertFlag(t, argv, "--resync")
}

func TestBisyncArgsForce(t *testing.T) {
	argv := newTestClient().BisyncArgs(Local("/local"), Remote("remote", "bucket"),
		BisyncOptions{Force: true})
	assertFlag(t, argv, "--force")
}

// ---------------------------------------------------------------------------
// Execution (TestRcloneCommandExecution: 13 tests)
// ---------------------------------------------------------------------------

func TestCopySuccess(t *testing.T) {
	c, _ := clientReturning(0, "", "")
	out, err := c.Copy(context.Background(), Local("/src"), Local("/dst"), TransferOptions{})
	if err != nil {
		t.Fatalf("Copy: %v", err)
	}
	if !out.OK {
		t.Fatal("Copy should report OK on exit 0")
	}
}

func TestCopyFailure(t *testing.T) {
	c, _ := clientReturning(1, "", "error")
	out, err := c.Copy(context.Background(), Local("/src"), Local("/dst"), TransferOptions{})
	if err != nil {
		t.Fatalf("a non-zero rclone exit is the caller's business, not an error: %v", err)
	}
	if out.OK {
		t.Fatal("Copy should not report OK on exit 1")
	}
	if out.Stderr != "error" {
		t.Fatalf("Stderr = %q, want %q", out.Stderr, "error")
	}
}

// TestCopyTransfersAreUnbounded pins the timeout policy: a transfer must be
// given no wall-clock ceiling, because a big box legitimately takes hours.
func TestCopyTransfersAreUnbounded(t *testing.T) {
	var timeouts []time.Duration
	c := newTestClient()
	c.Exec = func(_ context.Context, _ []string, timeout time.Duration) (runner.Result, error) {
		timeouts = append(timeouts, timeout)
		return runner.Result{}, nil
	}

	ctx := context.Background()
	if _, err := c.Copy(ctx, Local("/a"), Local("/b"), TransferOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := c.Sync(ctx, Local("/a"), Local("/b"), SyncOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.Bisync(ctx, Local("/a"), Local("/b"), BisyncOptions{}); err != nil {
		t.Fatal(err)
	}
	for i, got := range timeouts {
		if got != 0 {
			t.Errorf("transfer %d was given a %v ceiling; transfers must be unbounded", i, got)
		}
	}
}

// TestBoundedOperationsUseTheListingTimeout is the other half of that policy.
func TestBoundedOperationsUseTheListingTimeout(t *testing.T) {
	var timeouts []time.Duration
	c := newTestClient()
	c.Exec = func(_ context.Context, _ []string, timeout time.Duration) (runner.Result, error) {
		timeouts = append(timeouts, timeout)
		return runner.Result{Stdout: "[]"}, nil
	}

	ctx := context.Background()
	if err := c.Mkdir(ctx, Remote("r", "d")); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.Lsjson(ctx, Remote("r", "d"), LsjsonOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.Cat(ctx, Remote("r", "f")); err != nil {
		t.Fatal(err)
	}
	for i, got := range timeouts {
		if got != ListingTimeout {
			t.Errorf("bounded operation %d got a %v ceiling, want %v", i, got, ListingTimeout)
		}
	}
	if ListingTimeout != 600*time.Second {
		t.Fatalf("ListingTimeout = %v, want 600s (from boxconst %v)",
			ListingTimeout, boxconst.RcloneListingTimeoutSeconds)
	}
}

func TestMkdirErrorsOnFailure(t *testing.T) {
	c, _ := clientReturning(1, "", "mkdir failed")
	err := c.Mkdir(context.Background(), Remote("remote", "bucket/newdir"))
	if err == nil {
		t.Fatal("Mkdir must fail loudly")
	}
	if !strings.Contains(err.Error(), "mkdir failed") {
		t.Fatalf("error must carry rclone's own words, got %q", err)
	}
}

func TestLsjsonReturnsParsedJSON(t *testing.T) {
	c, _ := clientReturning(0, `[{"Name": "file.txt", "Size": 100}]`, "")
	entries, found, err := c.Lsjson(context.Background(), Remote("remote", "bucket"), LsjsonOptions{})
	if err != nil {
		t.Fatalf("Lsjson: %v", err)
	}
	if !found {
		t.Fatal("found = false, want true")
	}
	if len(entries) != 1 || entries[0].Name != "file.txt" || entries[0].Size != 100 {
		t.Fatalf("entries = %+v", entries)
	}
}

// TestLsjsonIgnoresUnknownFields: rclone emits far more fields than boxyard
// models, and adds more between versions. An unknown field must not break a
// listing.
func TestLsjsonIgnoresUnknownFields(t *testing.T) {
	stdout := `[{"Path":"a/b.txt","Name":"b.txt","Size":3,"MimeType":"text/plain",` +
		`"ModTime":"2026-08-22T22:59:35.358933561Z","IsDir":false,"Tier":"STANDARD",` +
		`"Hashes":{"md5":"x"},"SomeFutureRcloneField":42}]`
	c, _ := clientReturning(0, stdout, "")
	entries, _, err := c.Lsjson(context.Background(), Remote("r", "d"), LsjsonOptions{})
	if err != nil {
		t.Fatalf("Lsjson: %v", err)
	}
	if entries[0].Path != "a/b.txt" || entries[0].ModTime == "" {
		t.Fatalf("entries = %+v", entries)
	}
}

// TestLsjsonMissingPathIsNotAnError is where the Python returns None. Note that
// rclone prints a partial "[" to stdout before failing this way, so the exit
// code has to be what decides.
func TestLsjsonMissingPathIsNotAnError(t *testing.T) {
	for _, code := range []int{exitDirNotFound, exitFileNotFound} {
		c, _ := clientReturning(code, "[\n", "ERROR : error listing: directory not found")
		entries, found, err := c.Lsjson(context.Background(), Remote("remote", "bucket"), LsjsonOptions{})
		if err != nil {
			t.Fatalf("exit %d should mean 'absent', not an error: %v", code, err)
		}
		if found || entries != nil {
			t.Fatalf("exit %d: found=%v entries=%v, want false/nil", code, found, entries)
		}
	}
}

// TestLsjsonOtherFailureIsLoud is the divergence from the Python, which returns
// None for every non-zero exit and so cannot tell an unreachable remote from an
// empty one.
func TestLsjsonOtherFailureIsLoud(t *testing.T) {
	c, _ := clientReturning(1, "", "Failed to lsjson: couldn't connect SSH: dial tcp: i/o timeout")
	_, _, err := c.Lsjson(context.Background(), Remote("remote", "bucket"), LsjsonOptions{})
	if err == nil {
		t.Fatal("an unreachable remote must not be reported as an empty listing")
	}
	if !strings.Contains(err.Error(), "i/o timeout") {
		t.Fatalf("error must carry rclone's own words, got %q", err)
	}
}

func TestLsjsonUnparseableJSONIsLoud(t *testing.T) {
	c, _ := clientReturning(0, "not json at all", "")
	_, _, err := c.Lsjson(context.Background(), Remote("remote", "bucket"), LsjsonOptions{})
	if err == nil {
		t.Fatal("an unparseable listing must be a loud error, never an empty result")
	}
}

func TestLsjonExecFailurePropagates(t *testing.T) {
	want := &runner.SuspendError{Argv: []string{"rclone", "lsjson"}}
	c := newTestClient()
	c.Exec = func(context.Context, []string, time.Duration) (runner.Result, error) {
		return runner.Result{}, want
	}
	_, _, err := c.Lsjson(context.Background(), Remote("r", "d"), LsjsonOptions{})
	var se *runner.SuspendError
	if !errors.As(err, &se) {
		t.Fatalf("err = %v, want the runner's *SuspendError to propagate", err)
	}
}

func TestPathExistsRoot(t *testing.T) {
	// "." and "" both name the root of a remote, which always exists and is
	// always a directory. No subprocess should run at all.
	for _, p := range []string{".", ""} {
		c := newTestClient()
		c.Exec = func(context.Context, []string, time.Duration) (runner.Result, error) {
			t.Fatalf("the root check must not shell out (path %q)", p)
			return runner.Result{}, nil
		}
		exists, isDir, err := c.PathExists(context.Background(), Remote("remote", p))
		if err != nil || !exists || !isDir {
			t.Fatalf("PathExists(%q) = %v, %v, %v; want true, true, nil", p, exists, isDir, err)
		}
	}
}

func TestPathExistsFound(t *testing.T) {
	c, calls := clientReturning(0, `[{"Name": "mydir", "IsDir": true}]`, "")
	exists, isDir, err := c.PathExists(context.Background(), Remote("remote", "bucket/mydir"))
	if err != nil {
		t.Fatalf("PathExists: %v", err)
	}
	if !exists || !isDir {
		t.Fatalf("= %v, %v; want true, true", exists, isDir)
	}
	// It must have listed the PARENT, not the path itself.
	if len(*calls) != 1 {
		t.Fatalf("expected one lsjson call, got %d", len(*calls))
	}
	assertArg(t, (*calls)[0], "remote:bucket")
}

func TestPathExistsNotFound(t *testing.T) {
	c, _ := clientReturning(0, `[{"Name": "other", "IsDir": false}]`, "")
	exists, isDir, err := c.PathExists(context.Background(), Remote("remote", "bucket/missing"))
	if err != nil {
		t.Fatalf("PathExists: %v", err)
	}
	if exists || isDir {
		t.Fatalf("= %v, %v; want false, false", exists, isDir)
	}
}

func TestPurgeSuccess(t *testing.T) {
	c, _ := clientReturning(0, "", "")
	out, err := c.Purge(context.Background(), Remote("remote", "bucket/dir"))
	if err != nil || !out.OK {
		t.Fatalf("Purge = %+v, %v", out, err)
	}
}

func TestPurgeFailure(t *testing.T) {
	c, _ := clientReturning(1, "", "error")
	out, err := c.Purge(context.Background(), Remote("remote", "bucket/dir"))
	if err != nil {
		t.Fatalf("Purge: %v", err)
	}
	if out.OK {
		t.Fatal("Purge should not report OK on exit 1")
	}
}

func TestCatSuccess(t *testing.T) {
	c, _ := clientReturning(0, "file content", "")
	exists, content, err := c.Cat(context.Background(), Remote("remote", "bucket/file.txt"))
	if err != nil {
		t.Fatalf("Cat: %v", err)
	}
	if !exists || content != "file content" {
		t.Fatalf("= %v, %q", exists, content)
	}
}

func TestCatMissingFile(t *testing.T) {
	for _, code := range []int{exitDirNotFound, exitFileNotFound} {
		c, _ := clientReturning(code, "", "ERROR : error listing: directory not found")
		exists, content, err := c.Cat(context.Background(), Remote("remote", "bucket/missing.txt"))
		if err != nil {
			t.Fatalf("exit %d should mean 'absent', not an error: %v", code, err)
		}
		if exists || content != "" {
			t.Fatalf("exit %d: = %v, %q; want false, \"\"", code, exists, content)
		}
	}
}

// TestCatOtherFailureIsLoud: a sync record that could not be READ must never be
// reported as a sync record that is not THERE — the sync state machine treats
// those very differently.
func TestCatOtherFailureIsLoud(t *testing.T) {
	c, _ := clientReturning(5, "", "ERROR : couldn't connect SSH: connection reset by peer")
	_, _, err := c.Cat(context.Background(), Remote("remote", "bucket/sync_record.json"))
	if err == nil {
		t.Fatal("an unreadable remote file must not be reported as absent")
	}
	if !strings.Contains(err.Error(), "connection reset") {
		t.Fatalf("error must carry rclone's own words, got %q", err)
	}
}

func TestMoveSuccess(t *testing.T) {
	c, _ := clientReturning(0, "", "")
	out, err := c.Move(context.Background(), Local("/src"), Local("/dst"))
	if err != nil || !out.OK {
		t.Fatalf("Move = %+v, %v", out, err)
	}
}

// ---------------------------------------------------------------------------
// bisync classification (TestBisyncResultParsing: 5 tests)
// ---------------------------------------------------------------------------

func TestBisyncClassification(t *testing.T) {
	tests := []struct {
		name     string
		exitCode int
		stderr   string
		want     BisyncResult
	}{
		{"success", 0, "", BisyncSuccess},
		{"needs resync", 1, "ERROR : Bisync aborted. Must run --resync to recover.", BisyncNeedsResync},
		{"all files changed", 1, "ERROR : Safety abort: all files were changed", BisyncAllFilesChanged},
		{"conflicts", 0, "NOTICE: - WARNING  New or changed in both paths", BisyncConflicts},
		{"other error", 1, "Some other error", BisyncOtherError},
		// The recoverable aborts are recognised BEFORE the exit code, and the
		// conflicts notice only after it — that ordering is what stops a failed
		// run being reported as a successful one with conflicts.
		{"conflict notice on a failed run is an error", 1,
			"NOTICE: - WARNING  New or changed in both paths", BisyncOtherError},
		// rclone colours its output when it thinks a terminal is attached.
		{"needs resync, ANSI coloured", 1,
			"\x1b[31mERROR : Bisync aborted. Must run --resync to recover.\x1b[0m", BisyncNeedsResync},
		{"conflicts, ANSI coloured", 0,
			"\x1b[33mNOTICE: - WARNING  New or changed in both paths\x1b[0m", BisyncConflicts},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			c, _ := clientReturning(tc.exitCode, "", tc.stderr)
			got, out, err := c.Bisync(context.Background(), Local("/local"), Remote("remote", "bucket"),
				BisyncOptions{})
			if err != nil {
				t.Fatalf("Bisync: %v", err)
			}
			if got != tc.want {
				t.Fatalf("result = %q, want %q", got, tc.want)
			}
			// The raw streams are handed back untouched, so a caller can log
			// exactly what rclone said.
			if out.Stderr != tc.stderr {
				t.Fatalf("Stderr = %q, want %q", out.Stderr, tc.stderr)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// lsjson options (TestRcloneLsjsonOptions: 1 test, 5 sub-cases)
// ---------------------------------------------------------------------------

func TestLsjsonArgsOptions(t *testing.T) {
	c := newTestClient()
	loc := Remote("remote", "bucket")

	base := c.LsjsonArgs(loc, LsjsonOptions{})
	assertSubcommand(t, base, "lsjson")
	assertFlagValue(t, base, "--config", testConfig)
	assertArg(t, base, "remote:bucket")
	assertFlag(t, base, "--links")
	assertFlag(t, base, "--fast-list")
	assertNoFlag(t, base, "--dirs-only")
	assertNoFlag(t, base, "--files-only")
	assertNoFlag(t, base, "--recursive")
	assertNoFlag(t, base, "--max-depth")

	assertFlag(t, c.LsjsonArgs(loc, LsjsonOptions{DirsOnly: true}), "--dirs-only")
	assertFlag(t, c.LsjsonArgs(loc, LsjsonOptions{FilesOnly: true}), "--files-only")
	assertFlag(t, c.LsjsonArgs(loc, LsjsonOptions{Recursive: true}), "--recursive")
	assertFlagValue(t, c.LsjsonArgs(loc, LsjsonOptions{MaxDepth: 2}), "--max-depth", "2")

	filtered := c.LsjsonArgs(loc, LsjsonOptions{Filter: []string{"+ *.py", "- *"}})
	assertFlagValue(t, filtered, "--filter", "+ *.py")
	assertFlagValue(t, filtered, "--filter", "- *")
}

// ---------------------------------------------------------------------------
// mkdir (TestRcloneMkdir: 3 tests)
// ---------------------------------------------------------------------------

func TestMkdirBuildsCorrectCommand(t *testing.T) {
	c, calls := clientReturning(0, "", "")
	if err := c.Mkdir(context.Background(), Remote("remote", "bucket/newdir")); err != nil {
		t.Fatalf("Mkdir: %v", err)
	}
	if len(*calls) != 1 {
		t.Fatalf("expected one call, got %d", len(*calls))
	}
	argv := (*calls)[0]
	assertSubcommand(t, argv, "mkdir")
	assertFlagValue(t, argv, "--config", testConfig)
	assertArg(t, argv, "remote:bucket/newdir")
}

func TestMkdirLocalPath(t *testing.T) {
	c, calls := clientReturning(0, "", "")
	if err := c.Mkdir(context.Background(), Local("/local/newdir")); err != nil {
		t.Fatalf("Mkdir: %v", err)
	}
	argv := (*calls)[0]
	assertArg(t, argv, "/local/newdir")
	if strings.Contains(argv[len(argv)-1], ":") {
		t.Fatalf("a local path must not carry a remote prefix: %q", argv[len(argv)-1])
	}
}

func TestMkdirPermissionDenied(t *testing.T) {
	c, _ := clientReturning(1, "", "Permission denied")
	err := c.Mkdir(context.Background(), Remote("remote", "bucket/newdir"))
	if err == nil || !strings.Contains(err.Error(), "Permission denied") {
		t.Fatalf("err = %v, want an error naming the permission failure", err)
	}
}

// ---------------------------------------------------------------------------
// path exists (TestRclonePathExists: 5 tests)
// ---------------------------------------------------------------------------

func TestPathExistsAsDirectory(t *testing.T) {
	c, _ := clientReturning(0, `[{"Name":"mydir","IsDir":true},{"Name":"file.txt","IsDir":false}]`, "")
	exists, isDir, err := c.PathExists(context.Background(), Remote("remote", "bucket/mydir"))
	if err != nil || !exists || !isDir {
		t.Fatalf("= %v, %v, %v; want true, true, nil", exists, isDir, err)
	}
}

func TestPathExistsAsFile(t *testing.T) {
	c, _ := clientReturning(0, `[{"Name":"mydir","IsDir":true},{"Name":"file.txt","IsDir":false}]`, "")
	exists, isDir, err := c.PathExists(context.Background(), Remote("remote", "bucket/file.txt"))
	if err != nil || !exists || isDir {
		t.Fatalf("= %v, %v, %v; want true, false, nil", exists, isDir, err)
	}
}

func TestPathDoesNotExist(t *testing.T) {
	c, _ := clientReturning(0, `[{"Name":"other.txt","IsDir":false}]`, "")
	exists, isDir, err := c.PathExists(context.Background(), Remote("remote", "bucket/missing.txt"))
	if err != nil || exists || isDir {
		t.Fatalf("= %v, %v, %v; want false, false, nil", exists, isDir, err)
	}
}

func TestPathExistsParentDoesNotExist(t *testing.T) {
	c, _ := clientReturning(exitDirNotFound, "[\n", "ERROR : error listing: directory not found")
	exists, isDir, err := c.PathExists(context.Background(), Remote("remote", "nonexistent/path/file.txt"))
	if err != nil {
		t.Fatalf("a missing parent is an answer, not an error: %v", err)
	}
	if exists || isDir {
		t.Fatalf("= %v, %v; want false, false", exists, isDir)
	}
}

// TestPathExistsParentSelection pins which directory gets listed for each shape
// of path — the pathlib semantics the Python relies on.
func TestPathExistsParentSelection(t *testing.T) {
	tests := []struct {
		path       string
		wantParent string
	}{
		{"bucket/mydir", "bucket"},
		{"mydir", ""},             // single component: the remote's root
		{"/local/path", "/local"}, // absolute: the root is its own part
		{"/a", "/"},               //
		{"a/b/c", "a/b"},          //
		{"boxyard/boxes/x__y", "boxyard/boxes"},
	}
	for _, tc := range tests {
		t.Run(tc.path, func(t *testing.T) {
			c, calls := clientReturning(0, "[]", "")
			if _, _, err := c.PathExists(context.Background(), Remote("remote", tc.path)); err != nil {
				t.Fatalf("PathExists: %v", err)
			}
			argv := (*calls)[0]
			want := "remote:" + tc.wantParent
			assertArg(t, argv, want)
		})
	}
}

// TestPathExistsHandlesSpaces: the parent/name split must survive a box name
// with spaces, and the parent must reach rclone as one argument.
func TestPathExistsHandlesSpaces(t *testing.T) {
	c, calls := clientReturning(0, `[{"Name":"my box","IsDir":true}]`, "")
	exists, isDir, err := c.PathExists(context.Background(), Remote("remote", "boxyard/boxes/my box"))
	if err != nil || !exists || !isDir {
		t.Fatalf("= %v, %v, %v", exists, isDir, err)
	}
	assertArg(t, (*calls)[0], "remote:boxyard/boxes")
}

// ---------------------------------------------------------------------------
// purge (TestRclonePurge: 3 tests)
// ---------------------------------------------------------------------------

func TestPurgeBuildsCorrectCommand(t *testing.T) {
	c, calls := clientReturning(0, "", "")
	out, err := c.Purge(context.Background(), Remote("remote", "bucket/dir"))
	if err != nil || !out.OK {
		t.Fatalf("Purge = %+v, %v", out, err)
	}
	argv := (*calls)[0]
	assertSubcommand(t, argv, "purge")
	assertFlagValue(t, argv, "--config", testConfig)
	assertArg(t, argv, "remote:bucket/dir")
}

func TestPurgeLocalPath(t *testing.T) {
	c, calls := clientReturning(0, "", "")
	if _, err := c.Purge(context.Background(), Local("/local/dir")); err != nil {
		t.Fatalf("Purge: %v", err)
	}
	assertArg(t, (*calls)[0], "/local/dir")
}

func TestPurgeMissingDirectory(t *testing.T) {
	c, _ := clientReturning(1, "", "Directory not found")
	out, err := c.Purge(context.Background(), Remote("remote", "bucket/missing"))
	if err != nil {
		t.Fatalf("Purge: %v", err)
	}
	if out.OK {
		t.Fatal("Purge should not report OK when the directory is missing")
	}
}

// ---------------------------------------------------------------------------
// cat (TestRcloneCat: 3 tests)
// ---------------------------------------------------------------------------

func TestCatBuildsCorrectCommand(t *testing.T) {
	c, calls := clientReturning(0, "file contents", "")
	exists, content, err := c.Cat(context.Background(), Remote("remote", "bucket/file.txt"))
	if err != nil || !exists || content != "file contents" {
		t.Fatalf("= %v, %q, %v", exists, content, err)
	}
	argv := (*calls)[0]
	assertSubcommand(t, argv, "cat")
	assertFlagValue(t, argv, "--config", testConfig)
	assertArg(t, argv, "remote:bucket/file.txt")
}

func TestCatLocalFile(t *testing.T) {
	c, calls := clientReturning(0, "local content", "")
	exists, content, err := c.Cat(context.Background(), Local("/local/file.txt"))
	if err != nil || !exists || content != "local content" {
		t.Fatalf("= %v, %q, %v", exists, content, err)
	}
	assertArg(t, (*calls)[0], "/local/file.txt")
}

// ---------------------------------------------------------------------------
// move / moveto (TestRcloneMove: 4 tests)
// ---------------------------------------------------------------------------

func TestMoveBuildsCorrectCommand(t *testing.T) {
	c, calls := clientReturning(0, "", "")
	out, err := c.Move(context.Background(),
		Remote("remote1", "bucket1/file.txt"), Remote("remote2", "bucket2/file.txt"))
	if err != nil || !out.OK {
		t.Fatalf("Move = %+v, %v", out, err)
	}
	argv := (*calls)[0]
	assertSubcommand(t, argv, "move")
	assertFlagValue(t, argv, "--config", testConfig)
	assertArg(t, argv, "remote1:bucket1/file.txt")
	assertArg(t, argv, "remote2:bucket2/file.txt")
}

func TestMoveLocalToRemote(t *testing.T) {
	c, calls := clientReturning(0, "", "")
	if _, err := c.Move(context.Background(),
		Local("/local/file.txt"), Remote("remote", "bucket/file.txt")); err != nil {
		t.Fatalf("Move: %v", err)
	}
	argv := (*calls)[0]
	assertArg(t, argv, "/local/file.txt")
	assertArg(t, argv, "remote:bucket/file.txt")
}

func TestMoveRemoteToLocal(t *testing.T) {
	c, calls := clientReturning(0, "", "")
	if _, err := c.Move(context.Background(),
		Remote("remote", "bucket/file.txt"), Local("/local/file.txt")); err != nil {
		t.Fatalf("Move: %v", err)
	}
	argv := (*calls)[0]
	assertArg(t, argv, "remote:bucket/file.txt")
	assertArg(t, argv, "/local/file.txt")
}

func TestMoveReturnsStderrOnFailure(t *testing.T) {
	c, _ := clientReturning(1, "", "Permission denied")
	out, err := c.Move(context.Background(),
		Remote("remote", "bucket/file.txt"), Remote("remote2", "bucket2/file.txt"))
	if err != nil {
		t.Fatalf("Move: %v", err)
	}
	if out.OK {
		t.Fatal("Move should not report OK on exit 1")
	}
	if out.Stderr != "Permission denied" {
		t.Fatalf("Stderr = %q, want %q", out.Stderr, "Permission denied")
	}
}

func TestMovetoBuildsCorrectCommand(t *testing.T) {
	c, calls := clientReturning(0, "", "")
	if _, err := c.Moveto(context.Background(),
		Remote("r", "boxes/old__name"), Remote("r", "boxes/new__name")); err != nil {
		t.Fatalf("Moveto: %v", err)
	}
	argv := (*calls)[0]
	assertSubcommand(t, argv, "moveto")
	assertArg(t, argv, "r:boxes/old__name")
	assertArg(t, argv, "r:boxes/new__name")
}

func TestDeleteBuildsCorrectCommand(t *testing.T) {
	c, calls := clientReturning(0, "", "")
	out, err := c.Delete(context.Background(), Remote("r", "tombstones/x.json"))
	if err != nil || !out.OK {
		t.Fatalf("Delete = %+v, %v", out, err)
	}
	argv := (*calls)[0]
	assertSubcommand(t, argv, "deletefile")
	assertFlagValue(t, argv, "--config", testConfig)
	assertArg(t, argv, "r:tombstones/x.json")
}

// ---------------------------------------------------------------------------
// Write
// ---------------------------------------------------------------------------

func TestWriteStagesContentAndCopies(t *testing.T) {
	var staged string
	var seen []string
	c := newTestClient()
	c.Exec = func(_ context.Context, argv []string, _ time.Duration) (runner.Result, error) {
		seen = argv
		// argv is [rclone copyto --config cfg <tmp> <dest>].
		data, err := os.ReadFile(argv[len(argv)-2])
		if err != nil {
			t.Errorf("staging file unreadable: %v", err)
		}
		staged = string(data)
		return runner.Result{}, nil
	}

	out, err := c.Write(context.Background(), Remote("r", "sync_records/x.json"), `{"ulid":"01J"}`)
	if err != nil || !out.OK {
		t.Fatalf("Write = %+v, %v", out, err)
	}
	if staged != `{"ulid":"01J"}` {
		t.Fatalf("staged content = %q", staged)
	}
	assertSubcommand(t, seen, "copyto")
	assertArg(t, seen, "r:sync_records/x.json")

	// The staging file must not survive the call.
	if _, err := os.Stat(seen[len(seen)-2]); !os.IsNotExist(err) {
		t.Fatalf("staging file %s was left behind", seen[len(seen)-2])
	}
}

// ---------------------------------------------------------------------------
// StripANSI
// ---------------------------------------------------------------------------

func TestStripANSI(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{"plain text", "no escapes here", "no escapes here"},
		{"CSI colour", "\x1b[31mred\x1b[0m", "red"},
		{"CSI with parameters", "\x1b[1;33;40mbold\x1b[0m", "bold"},
		{"cursor movement", "a\x1b[2Kb", "ab"},
		{"7-bit C1", "\x1bMreverse index", "reverse index"},
		{"empty", "", ""},
		{"real bisync line", "\x1b[31mERROR : Bisync aborted. Must run --resync to recover.\x1b[0m",
			"ERROR : Bisync aborted. Must run --resync to recover."},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := StripANSI(tc.in); got != tc.want {
				t.Fatalf("StripANSI(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Binary resolution (test_rclone_resolution.py: 8 tests)
// ---------------------------------------------------------------------------

// makeFakeRclone writes an executable stub named rclone into dir.
func makeFakeRclone(t *testing.T, dir string) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", dir, err)
	}
	p := filepath.Join(dir, "rclone")
	if err := os.WriteFile(p, []byte("#!/bin/sh\necho fake\n"), 0o755); err != nil {
		t.Fatalf("write %s: %v", p, err)
	}
	return p
}

// testResolver returns a resolver with no environment, no config file and no
// PATH hit, so each test opts in to exactly the source it is exercising.
func testResolver(t *testing.T) resolver {
	t.Helper()
	return resolver{
		getenv:       func(string) string { return "" },
		lookPath:     func(string) (string, error) { return "", errors.New("not found") },
		isExecutable: isExecutable,
		fallbackDirs: nil,
		configPath:   filepath.Join(t.TempDir(), "nonexistent.toml"),
	}
}

func TestResolveEnvVarHonoured(t *testing.T) {
	fake := makeFakeRclone(t, filepath.Join(t.TempDir(), "custom"))
	r := testResolver(t)
	r.getenv = func(k string) string {
		if k == boxconst.EnvBoxyardRclone {
			return fake
		}
		return ""
	}
	// Even with a PATH hit, the env var takes priority.
	r.lookPath = func(string) (string, error) { return "/usr/bin/rclone", nil }

	got, err := r.resolve()
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if got != fake {
		t.Fatalf("resolve() = %q, want %q", got, fake)
	}
}

func TestResolveEnvVarInvalidIsLoud(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "does_not_exist")
	r := testResolver(t)
	r.getenv = func(k string) string {
		if k == boxconst.EnvBoxyardRclone {
			return missing
		}
		return ""
	}
	// A working PATH hit must NOT rescue an explicit setting that is wrong.
	r.lookPath = func(string) (string, error) { return "/usr/bin/rclone", nil }

	_, err := r.resolve()
	if err == nil {
		t.Fatal("a BOXYARD_RCLONE pointing at nothing must be an error, not a fall-through")
	}
	if !strings.Contains(err.Error(), boxconst.EnvBoxyardRclone) {
		t.Fatalf("error must name the variable, got %q", err)
	}
}

func TestResolveConfigRclonePathHonoured(t *testing.T) {
	dir := t.TempDir()
	fake := makeFakeRclone(t, filepath.Join(dir, "from_config"))
	configPath := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(configPath, fmt.Appendf(nil, "rclone_path = %q\n", fake), 0o644); err != nil {
		t.Fatal(err)
	}

	r := testResolver(t)
	r.configPath = configPath
	r.lookPath = func(string) (string, error) { return "/usr/bin/rclone", nil }

	got, err := r.resolve()
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if got != fake {
		t.Fatalf("resolve() = %q, want %q", got, fake)
	}
}

func TestResolveConfigRclonePathInvalidIsLoud(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.toml")
	missing := filepath.Join(dir, "missing")
	if err := os.WriteFile(configPath, fmt.Appendf(nil, "rclone_path = %q\n", missing), 0o644); err != nil {
		t.Fatal(err)
	}

	r := testResolver(t)
	r.configPath = configPath

	_, err := r.resolve()
	if err == nil || !strings.Contains(err.Error(), "rclone_path") {
		t.Fatalf("err = %v, want an error naming rclone_path", err)
	}
}

// TestResolveConfigReadsOnlyOneKey: locating rclone must not depend on the rest
// of the config being valid, or a typo'd key would be reported as a missing
// binary.
func TestResolveConfigReadsOnlyOneKey(t *testing.T) {
	dir := t.TempDir()
	fake := makeFakeRclone(t, filepath.Join(dir, "bin"))
	configPath := filepath.Join(dir, "config.toml")
	body := fmt.Sprintf("rclone_path = %q\nnot_a_real_boxyard_key = 1\nbox_subid_length = \"wrong type\"\n", fake)
	if err := os.WriteFile(configPath, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}

	r := testResolver(t)
	r.configPath = configPath
	got, err := r.resolve()
	if err != nil {
		t.Fatalf("an otherwise-invalid config must still yield rclone_path: %v", err)
	}
	if got != fake {
		t.Fatalf("resolve() = %q, want %q", got, fake)
	}
}

func TestResolveConfigMalformedTOMLIsLoud(t *testing.T) {
	dir := t.TempDir()
	configPath := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(configPath, []byte("this is not = = toml\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	r := testResolver(t)
	r.configPath = configPath
	if _, err := r.resolve(); err == nil {
		t.Fatal("a malformed config.toml must be a loud error")
	}
}

func TestResolveUsesPATH(t *testing.T) {
	r := testResolver(t)
	r.lookPath = func(name string) (string, error) {
		if name != "rclone" {
			t.Fatalf("lookPath(%q)", name)
		}
		return "/path/from/which/rclone", nil
	}
	got, err := r.resolve()
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if got != "/path/from/which/rclone" {
		t.Fatalf("resolve() = %q", got)
	}
}

func TestResolveFallbackDirs(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "fallback")
	fake := makeFakeRclone(t, dir)
	r := testResolver(t)
	r.fallbackDirs = []string{dir}

	got, err := r.resolve()
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if got != fake {
		t.Fatalf("resolve() = %q, want %q", got, fake)
	}
}

// TestResolveNotFoundNamesEveryLocation: the error is the whole point of the
// resolver — a bare "executable file not found in $PATH" tells the user
// nothing about the three other places boxyard looked.
func TestResolveNotFoundNamesEveryLocation(t *testing.T) {
	emptyDir := filepath.Join(t.TempDir(), "empty")
	if err := os.MkdirAll(emptyDir, 0o755); err != nil {
		t.Fatal(err)
	}
	r := testResolver(t)
	r.fallbackDirs = []string{emptyDir}

	_, err := r.resolve()
	if err == nil {
		t.Fatal("expected an error")
	}
	msg := err.Error()
	for _, want := range []string{
		boxconst.EnvBoxyardRclone,
		"config.toml",
		"PATH",
		filepath.Join(emptyDir, "rclone"),
		"install",
		"https://rclone.org/install/",
	} {
		if !strings.Contains(msg, want) {
			t.Errorf("error should mention %q; got:\n%s", want, msg)
		}
	}
}

// TestBinaryCachesResolution proves the cache behaviourally: after the first
// call, changing what resolution WOULD find has no effect.
func TestBinaryCachesResolution(t *testing.T) {
	binaryMu.Lock()
	saved := binaryCache
	binaryCache = ""
	binaryMu.Unlock()
	t.Cleanup(func() {
		binaryMu.Lock()
		binaryCache = saved
		binaryMu.Unlock()
	})

	fake := makeFakeRclone(t, filepath.Join(t.TempDir(), "first"))
	t.Setenv(boxconst.EnvBoxyardRclone, fake)

	first, err := Binary()
	if err != nil {
		t.Fatalf("Binary: %v", err)
	}
	if first != fake {
		t.Fatalf("Binary() = %q, want %q", first, fake)
	}

	// Point the environment somewhere that would fail to resolve. A cached
	// value must be returned without re-resolving.
	t.Setenv(boxconst.EnvBoxyardRclone, filepath.Join(t.TempDir(), "gone"))
	second, err := Binary()
	if err != nil {
		t.Fatalf("Binary (second call): %v", err)
	}
	if second != first {
		t.Fatalf("Binary() = %q on the second call, want the cached %q", second, first)
	}
}

// TestBinaryDoesNotCacheFailures: installing rclone and retrying must work
// without restarting a long-running supervisor.
func TestBinaryDoesNotCacheFailures(t *testing.T) {
	binaryMu.Lock()
	saved := binaryCache
	binaryCache = ""
	binaryMu.Unlock()
	t.Cleanup(func() {
		binaryMu.Lock()
		binaryCache = saved
		binaryMu.Unlock()
	})

	dir := t.TempDir()
	target := filepath.Join(dir, "rclone")
	t.Setenv(boxconst.EnvBoxyardRclone, target)

	if _, err := Binary(); err == nil {
		t.Fatal("expected the first call to fail")
	}
	makeFakeRclone(t, dir)
	got, err := Binary()
	if err != nil {
		t.Fatalf("Binary after the binary appeared: %v", err)
	}
	if got != target {
		t.Fatalf("Binary() = %q, want %q", got, target)
	}
}

// ---------------------------------------------------------------------------
// POSIX path helpers
// ---------------------------------------------------------------------------

func TestPosixParts(t *testing.T) {
	tests := []struct {
		in   string
		want []string
	}{
		{"", nil},
		{".", nil},
		{"a", []string{"a"}},
		{"a/b", []string{"a", "b"}},
		{"a//b", []string{"a", "b"}},
		{"./a", []string{"a"}},
		{"a/.", []string{"a"}},
		{"/", []string{"/"}},
		{"/a", []string{"/", "a"}},
		{"/a/b", []string{"/", "a", "b"}},
		{"my box/data", []string{"my box", "data"}},
	}
	for _, tc := range tests {
		if got := posixParts(tc.in); !slices.Equal(got, tc.want) {
			t.Errorf("posixParts(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestPosixString(t *testing.T) {
	tests := map[string]string{
		"":     ".",
		".":    ".",
		"a":    "a",
		"a/b":  "a/b",
		"/":    "/",
		"/a/b": "/a/b",
		"./a":  "a",
		"a//b": "a/b",
	}
	for in, want := range tests {
		if got := posixString(in); got != want {
			t.Errorf("posixString(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestLocationSpec(t *testing.T) {
	tests := []struct {
		loc  Location
		want string
	}{
		{Local("/a/b"), "/a/b"},
		{Remote("r", "a/b"), "r:a/b"},
		{Remote("r", ""), "r:"},
		{Remote("hetzner-box", "boxyard/boxes/my box"), "hetzner-box:boxyard/boxes/my box"},
	}
	for _, tc := range tests {
		if got := tc.loc.Spec(); got != tc.want {
			t.Errorf("%+v.Spec() = %q, want %q", tc.loc, got, tc.want)
		}
	}
}

func TestLocationJoin(t *testing.T) {
	got := Remote("r", "boxyard").Join("boxes", "20260101_ab1cd__my box", "data")
	want := "r:boxyard/boxes/20260101_ab1cd__my box/data"
	if got.Spec() != want {
		t.Fatalf("Join(...).Spec() = %q, want %q", got.Spec(), want)
	}
}

// ---------------------------------------------------------------------------
// Integration: the real rclone binary, LOCAL PATHS ONLY
//
// These validate the assumptions the error handling above rests on — most
// importantly that rclone really does signal an absent path with exit 3/4.
// They never define a network remote and never read the user's rclone config,
// so they cannot reach hetzner-box or any other real storage.
// ---------------------------------------------------------------------------

// realClient returns a client using the installed rclone and an EMPTY rclone
// config in a temp dir, so no remote is even defined. It skips if rclone is
// not installed.
func realClient(t *testing.T) *Client {
	t.Helper()
	bin, err := ResolveBinary()
	if err != nil {
		t.Skipf("rclone is not installed: %v", err)
	}
	cfg := filepath.Join(t.TempDir(), "empty_rclone.conf")
	if err := os.WriteFile(cfg, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	return NewWithBinary(bin, cfg)
}

func TestIntegrationMissingPathsUseExitCodeThreeOrFour(t *testing.T) {
	c := realClient(t)
	dir := t.TempDir()
	ctx := context.Background()

	// A missing directory.
	entries, found, err := c.Lsjson(ctx, Local(filepath.Join(dir, "nope")), LsjsonOptions{})
	if err != nil {
		t.Fatalf("Lsjson on a missing dir must not error (this is the exit 3/4 assumption): %v", err)
	}
	if found || entries != nil {
		t.Fatalf("found=%v entries=%v, want false/nil", found, entries)
	}

	// A missing file.
	exists, content, err := c.Cat(ctx, Local(filepath.Join(dir, "nope.txt")))
	if err != nil {
		t.Fatalf("Cat on a missing file must not error: %v", err)
	}
	if exists || content != "" {
		t.Fatalf("exists=%v content=%q", exists, content)
	}
}

func TestIntegrationRoundTrip(t *testing.T) {
	c := realClient(t)
	ctx := context.Background()
	root := t.TempDir()

	// A source tree with a space in a directory name, mirroring the real boxes.
	src := filepath.Join(root, "20260101_ab1cd__my box", "data")
	if err := os.MkdirAll(filepath.Join(src, "sub dir"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "a.txt"), []byte("alpha"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "sub dir", "b.txt"), []byte("beta"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, "skip.tmp"), []byte("nope"), 0o644); err != nil {
		t.Fatal(err)
	}

	dst := filepath.Join(root, "dest with space")
	if err := c.Mkdir(ctx, Local(dst)); err != nil {
		t.Fatalf("Mkdir: %v", err)
	}

	out, err := c.Copy(ctx, Local(src), Local(dst), TransferOptions{Exclude: []string{"*.tmp"}})
	if err != nil || !out.OK {
		t.Fatalf("Copy = %+v, %v", out, err)
	}

	// The exclude must have been honoured, which only works if the pattern
	// reached rclone as one argument.
	if _, err := os.Stat(filepath.Join(dst, "skip.tmp")); !os.IsNotExist(err) {
		t.Fatal("--exclude '*.tmp' was not honoured")
	}
	if data, err := os.ReadFile(filepath.Join(dst, "sub dir", "b.txt")); err != nil || string(data) != "beta" {
		t.Fatalf("copied file = %q, %v", data, err)
	}

	// PathExists across a name with spaces.
	exists, isDir, err := c.PathExists(ctx, Local(filepath.Join(dst, "sub dir")))
	if err != nil || !exists || !isDir {
		t.Fatalf("PathExists(sub dir) = %v, %v, %v", exists, isDir, err)
	}
	exists, isDir, err = c.PathExists(ctx, Local(filepath.Join(dst, "a.txt")))
	if err != nil || !exists || isDir {
		t.Fatalf("PathExists(a.txt) = %v, %v, %v", exists, isDir, err)
	}
	exists, _, err = c.PathExists(ctx, Local(filepath.Join(dst, "not there")))
	if err != nil || exists {
		t.Fatalf("PathExists(not there) = %v, %v", exists, err)
	}

	// Write, Cat, Delete round trip.
	target := Local(filepath.Join(dst, "sync record.json"))
	if out, err := c.Write(ctx, target, `{"sync_complete":true}`); err != nil || !out.OK {
		t.Fatalf("Write = %+v, %v", out, err)
	}
	found, content, err := c.Cat(ctx, target)
	if err != nil || !found || content != `{"sync_complete":true}` {
		t.Fatalf("Cat = %v, %q, %v", found, content, err)
	}
	if out, err := c.Delete(ctx, target); err != nil || !out.OK {
		t.Fatalf("Delete = %+v, %v", out, err)
	}
	if found, _, err := c.Cat(ctx, target); err != nil || found {
		t.Fatalf("after Delete, Cat = %v, %v", found, err)
	}

	// Purge removes the tree.
	if out, err := c.Purge(ctx, Local(dst)); err != nil || !out.OK {
		t.Fatalf("Purge = %+v, %v", out, err)
	}
	if _, err := os.Stat(dst); !os.IsNotExist(err) {
		t.Fatalf("Purge left %s behind", dst)
	}
}

// TestIntegrationLsjsonListsRecursively covers the options the remote-index and
// tombstone readers depend on.
func TestIntegrationLsjsonOptions(t *testing.T) {
	c := realClient(t)
	ctx := context.Background()
	root := t.TempDir()

	if err := os.MkdirAll(filepath.Join(root, "boxes", "20260101_ab1cd__x"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "boxes", "top.txt"), []byte("t"), 0o644); err != nil {
		t.Fatal(err)
	}

	dirs, found, err := c.Lsjson(ctx, Local(filepath.Join(root, "boxes")), LsjsonOptions{DirsOnly: true})
	if err != nil || !found {
		t.Fatalf("Lsjson: %v (found=%v)", err, found)
	}
	if len(dirs) != 1 || dirs[0].Name != "20260101_ab1cd__x" || !dirs[0].IsDir {
		t.Fatalf("dirs = %+v", dirs)
	}

	files, _, err := c.Lsjson(ctx, Local(filepath.Join(root, "boxes")), LsjsonOptions{FilesOnly: true})
	if err != nil {
		t.Fatalf("Lsjson: %v", err)
	}
	if len(files) != 1 || files[0].Name != "top.txt" {
		t.Fatalf("files = %+v", files)
	}
}

// ---------------------------------------------------------------------------
// check
// ---------------------------------------------------------------------------

// The exact argv Python's `_rclone_cmd_helper("check", ...) + ["--combined",
// "-"]` produces for the same inputs, captured by running it:
//
//	['/usr/bin/rclone', 'check', '--config', '/tmp/rclone.conf', '--links',
//	 '/local', 'hetzner:boxes/x/data', '--fast-list', '--include-from', '/inc',
//	 '--exclude-from', '/exc', '--filters-file', '/flt', '--combined', '-']
func TestCheckArgsMatchesPython(t *testing.T) {
	argv := newTestClient().CheckArgs(Local("/local"), Remote("hetzner", "boxes/x/data"),
		TransferOptions{IncludeFile: "/inc", ExcludeFile: "/exc", FiltersFile: "/flt"})

	want := []string{
		"/usr/bin/rclone", "check", "--config", testConfig, "--links",
		"/local", "hetzner:boxes/x/data", "--fast-list",
		"--include-from", "/inc", "--exclude-from", "/exc",
		"--filters-file", "/flt", "--combined", "-",
	}
	if !slices.Equal(argv, want) {
		t.Fatalf("argv mismatch\n got: %q\nwant: %q", argv, want)
	}
}

// A read-only comparison has no dry run and no progress bar. Emitting either
// would diverge from the Python, which hardcodes False for both.
func TestCheckArgsIgnoresDryRunAndProgress(t *testing.T) {
	argv := newTestClient().CheckArgs(Local("/a"), Local("/b"),
		TransferOptions{DryRun: true, Progress: true})
	for _, unwanted := range []string{"--dry-run", "--progress"} {
		if slices.Contains(argv, unwanted) {
			t.Errorf("check must not pass %s: %q", unwanted, argv)
		}
	}
}

func TestCheckParsesCombinedOutput(t *testing.T) {
	cases := []struct {
		name         string
		exitCode     int
		stdout       string
		wantAnswered bool
		wantDiffer   []string
	}{
		{
			name:         "everything identical",
			exitCode:     0,
			stdout:       "= a.txt\n= b/c.txt\n",
			wantAnswered: true,
		},
		{
			// Both sides empty produces no lines at all, and exit 0 says the
			// comparison DID run.
			name:         "both sides empty",
			exitCode:     0,
			stdout:       "",
			wantAnswered: true,
		},
		{
			name:         "differences found",
			exitCode:     1,
			stdout:       "= same.txt\n+ only-local.txt\n* differs.txt\n- only-remote.txt\n",
			wantAnswered: true,
			wantDiffer:   []string{"only-local.txt", "differs.txt", "only-remote.txt"},
		},
		{
			// rclone reports "found differences" and "could not look" with the
			// SAME exit code. Only the absence of any line separates them, and
			// reading this as "no differences" is the one thing the probe must
			// never do.
			name:         "could not look",
			exitCode:     1,
			stdout:       "",
			wantAnswered: false,
		},
		{
			name:         "blank lines are ignored",
			exitCode:     0,
			stdout:       "\n= a.txt\n\n",
			wantAnswered: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var calls [][]string
			c := newTestClient()
			c.Exec = fakeRun(runner.Result{ExitCode: tc.exitCode, Stdout: tc.stdout}, &calls)

			answered, differing, err := c.Check(context.Background(), Local("/a"), Local("/b"), TransferOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if answered != tc.wantAnswered {
				t.Fatalf("answered = %v, want %v", answered, tc.wantAnswered)
			}
			if !slices.Equal(differing, tc.wantDiffer) {
				t.Fatalf("differing = %q, want %q", differing, tc.wantDiffer)
			}
		})
	}
}

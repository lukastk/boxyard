// Package rclone wraps the rclone binary.
//
// Ported from src/boxyard/_utils/rclone.py. rclone does all of boxyard's real
// I/O — there is no S3 or SFTP client here — so this package is an argv builder
// plus output parsing over about a dozen subcommands, run through
// internal/runner.
//
// # ARGV, NEVER A SHELL STRING
//
// Nothing here builds a command by concatenating strings. Every path reaches
// rclone as its own argv element. Two of the user's real boxes have SPACES in
// their names, and box names come from the filesystem, so a command assembled
// by interpolation would be at best broken and at worst executable.
//
// # REMOTE PATHS ARE NOT FILESYSTEM PATHS
//
// A remote path is always POSIX and always relative to a remote's root, so path
// manipulation here uses "path" (and the pathlib-shaped helpers at the bottom
// of this file), never "path/filepath". A Go binary running on any OS must
// produce the same remote paths as the Python implementation on five others.
//
// # WHAT COUNTS AS AN ERROR
//
// rclone reports "the thing is not there" through exit codes 3 (directory not
// found) and 4 (file not found), and that is a legitimate expected state for a
// listing or a read: it is how PathExists answers its question. Every OTHER
// non-zero exit is a real failure and is returned as an error.
//
// The Python conflates the two — rclone_lsjson and rclone_cat return None for
// any non-zero exit — so a transient SFTP failure is indistinguishable there
// from an empty remote. That conflation has teeth: it is what lets
// scan_and_rebuild_remote_index_cache persist an EMPTY index after a network
// blip. This package does not reproduce it; see PARITY-NOTES.md.
//
// Commands that MUTATE (copy, sync, purge, move, deletefile) keep the Python's
// shape instead: they return an Output whose OK field carries rclone's verdict,
// and the caller decides. Their failures are the caller's business, not this
// layer's.
package rclone

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/lukastk/boxyard/internal/boxconst"
	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/runner"
	"github.com/pelletier/go-toml/v2"
)

// ListingTimeout is the wall-clock ceiling applied to rclone calls whose work
// is inherently bounded — listings and metadata reads. Transfers are NOT bounded
// by it: a big box legitimately takes hours, and the suspend watchdog in
// internal/runner covers those instead.
const ListingTimeout = time.Duration(boxconst.RcloneListingTimeoutSeconds * float64(time.Second))

// rclone's exit codes for an absent path. These are the only non-zero exits
// this package treats as an answer rather than a failure.
const (
	exitDirNotFound  = 3
	exitFileNotFound = 4
)

// ---------------------------------------------------------------------------
// Locations
// ---------------------------------------------------------------------------

// Location is one end of an rclone operation: a remote name and a path within
// it. An empty Remote means the local filesystem.
type Location struct {
	Remote string
	Path   string
}

// Local returns a Location on the local filesystem.
func Local(p string) Location { return Location{Path: p} }

// Remote returns a Location on the named rclone remote.
func Remote(remote, p string) Location { return Location{Remote: remote, Path: p} }

// Spec renders the location the way rclone takes it on the command line:
// "remote:path", or a bare path when there is no remote.
//
// This is one argv element, whatever it contains — spaces included.
func (l Location) Spec() string {
	if l.Remote == "" {
		return l.Path
	}
	return l.Remote + ":" + l.Path
}

// Join returns the location with elem appended to its path, joined as a POSIX
// remote path rather than an OS filesystem path.
func (l Location) Join(elem ...string) Location {
	l.Path = path.Join(append([]string{l.Path}, elem...)...)
	return l
}

// ---------------------------------------------------------------------------
// Binary resolution
// ---------------------------------------------------------------------------

// defaultFallbackDirs are the known install locations searched when rclone is
// not on PATH: Homebrew (Apple Silicon and Intel), the system dirs, and snap.
var defaultFallbackDirs = []string{
	"/opt/homebrew/bin", // Homebrew on Apple Silicon
	"/usr/local/bin",    // Homebrew on Intel macs / common manual installs
	"/usr/bin",
	"/bin",
	"/usr/sbin",
	"/sbin",
	"/snap/bin", // snap-installed rclone on Linux
}

// resolver locates the rclone binary. Its fields are the seams tests replace;
// newResolver installs the real ones.
type resolver struct {
	getenv       func(string) string
	lookPath     func(string) (string, error)
	isExecutable func(string) bool
	fallbackDirs []string

	// configPath, when set, is the boxyard config.toml consulted for the
	// optional rclone_path key. Empty means $BOXYARD_CONFIG_PATH, then the
	// default location — matching the Python.
	configPath string
}

func newResolver() resolver {
	return resolver{
		getenv:       os.Getenv,
		lookPath:     exec.LookPath,
		isExecutable: isExecutable,
		fallbackDirs: defaultFallbackDirs,
	}
}

// ResolveBinary locates the rclone binary, in this order:
//
//  1. the BOXYARD_RCLONE environment variable (an explicit full path)
//  2. the rclone_path key in the boxyard config.toml, if present
//  3. PATH
//  4. the known install dirs (Homebrew, system dirs, snap)
//
// A binary that cannot be found produces an error naming every location
// searched and every way to fix it — never a bare "executable file not found"
// at exec time. A location that IS configured but holds no executable is an
// error too: an explicit setting that does not work must never fall through to
// something else that happens to.
//
// Resolution is not cached; use Binary for that.
func ResolveBinary() (string, error) { return newResolver().resolve() }

func (r resolver) resolve() (string, error) {
	var searched []string

	// 1. Explicit environment override.
	if envPath := r.getenv(boxconst.EnvBoxyardRclone); envPath != "" {
		expanded, err := config.ExpandUser(envPath)
		if err != nil {
			return "", fmt.Errorf("%s is set to %q, which is not a usable path: %w",
				boxconst.EnvBoxyardRclone, envPath, err)
		}
		if r.isExecutable(expanded) {
			return expanded, nil
		}
		return "", fmt.Errorf("%s is set to '%s', but no executable rclone binary exists there. "+
			"Fix the path or unset %s.", boxconst.EnvBoxyardRclone, envPath, boxconst.EnvBoxyardRclone)
	}
	searched = append(searched, fmt.Sprintf("$%s (env var, unset)", boxconst.EnvBoxyardRclone))

	// 2. The rclone_path key in config.toml.
	configured, err := r.rclonePathFromConfig()
	if err != nil {
		return "", err
	}
	if configured != "" {
		if r.isExecutable(configured) {
			return configured, nil
		}
		return "", fmt.Errorf("`rclone_path` in the boxyard config.toml points to '%s', but no "+
			"executable rclone binary exists there. Fix or remove the `rclone_path` key.", configured)
	}
	searched = append(searched, "`rclone_path` in config.toml (unset)")

	// 3. The caller's PATH.
	if found, err := r.lookPath("rclone"); err == nil && found != "" {
		return found, nil
	}
	searched = append(searched, "PATH (via exec.LookPath)")

	// 4. Known install dirs.
	for _, dir := range r.fallbackDirs {
		candidate := path.Join(dir, "rclone")
		if r.isExecutable(candidate) {
			return candidate, nil
		}
		searched = append(searched, candidate)
	}

	return "", fmt.Errorf("boxyard could not find the `rclone` binary. Searched:\n  - %s\n\n"+
		"Install rclone (https://rclone.org/install/), or set %s to its full path "+
		"(e.g. %s=/opt/homebrew/bin/rclone), or add a `rclone_path` key to your boxyard config.toml.",
		strings.Join(searched, "\n  - "), boxconst.EnvBoxyardRclone, boxconst.EnvBoxyardRclone)
}

// rclonePathFromConfig reads just the optional rclone_path key out of the
// boxyard config.toml.
//
// It deliberately does NOT go through internal/strict or internal/config.
// Locating rclone must not depend on the rest of the config being valid — a
// config with a typo'd key still has to be able to tell us where rclone lives,
// so that the error the user sees is their typo and not a missing binary. This
// is the one place in the codebase that reads a boxyard file loosely, and it
// reads exactly one key. Malformed TOML is still an error.
func (r resolver) rclonePathFromConfig() (string, error) {
	configPath := r.configPath
	if configPath == "" {
		configPath = r.getenv(boxconst.EnvBoxyardConfigPath)
	}
	if configPath == "" {
		configPath = boxconst.DefaultConfigPath
	}
	expanded, err := config.ExpandUser(configPath)
	if err != nil {
		return "", err
	}

	data, err := os.ReadFile(expanded)
	if err != nil {
		if os.IsNotExist(err) {
			// No config file is a legitimate state: boxyard resolves rclone
			// before `boxyard init` has ever run.
			return "", nil
		}
		return "", fmt.Errorf("cannot read %s while looking for the rclone binary: %w", expanded, err)
	}

	var doc struct {
		RclonePath string `toml:"rclone_path"`
	}
	if err := toml.Unmarshal(data, &doc); err != nil {
		return "", fmt.Errorf("cannot read `rclone_path` from %s: %w", expanded, err)
	}
	if doc.RclonePath == "" {
		return "", nil
	}
	return config.ExpandUser(doc.RclonePath)
}

// isExecutable reports whether p is a regular file this process may execute.
// It asks the kernel (access(2)) rather than inspecting the mode bits, so a
// file that is +x for someone else is correctly reported as unusable.
func isExecutable(p string) bool {
	info, err := os.Stat(p)
	if err != nil || !info.Mode().IsRegular() {
		return false
	}
	return syscall.Access(p, 0x1 /* X_OK */) == nil
}

var (
	binaryMu    sync.Mutex
	binaryCache string
)

// Binary returns the resolved rclone binary path, resolving on first use and
// caching the result for the life of the process.
//
// Only a successful resolution is cached: a failure is re-attempted next time,
// so installing rclone and retrying works without restarting a long-running
// supervisor.
func Binary() (string, error) {
	binaryMu.Lock()
	defer binaryMu.Unlock()
	if binaryCache != "" {
		return binaryCache, nil
	}
	found, err := ResolveBinary()
	if err != nil {
		return "", err
	}
	binaryCache = found
	return found, nil
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

// Client runs rclone subcommands against one rclone config file.
//
// It is stateless beyond those two paths and safe for concurrent use; the
// concurrency limit lives in internal/runner.
type Client struct {
	// Binary is the resolved rclone binary.
	Binary string
	// ConfigPath is passed as --config to every subcommand.
	ConfigPath string

	// Exec is the subprocess seam. Leave it nil to use runner.Run; tests
	// substitute a fake so the argv builders can be exercised without spawning
	// anything.
	Exec runner.RunFunc
}

// New resolves the rclone binary and returns a Client bound to rcloneConfigPath.
func New(rcloneConfigPath string) (*Client, error) {
	bin, err := Binary()
	if err != nil {
		return nil, err
	}
	return &Client{Binary: bin, ConfigPath: rcloneConfigPath}, nil
}

// NewWithBinary returns a Client using an already-known rclone binary.
func NewWithBinary(binary, rcloneConfigPath string) *Client {
	return &Client{Binary: binary, ConfigPath: rcloneConfigPath}
}

func (c *Client) exec(ctx context.Context, argv []string, timeout time.Duration) (runner.Result, error) {
	run := c.Exec
	if run == nil {
		run = runner.Run
	}
	return run(ctx, argv, timeout)
}

// Output is the outcome of an rclone command the caller is expected to branch
// on: OK carries rclone's verdict, and the streams are kept so a failure can be
// reported with rclone's own words.
type Output struct {
	OK     bool
	Stdout string
	Stderr string
}

func outputOf(res runner.Result) Output {
	return Output{OK: res.OK(), Stdout: res.Stdout, Stderr: res.Stderr}
}

// failure renders a command failure using rclone's stderr, with ANSI escapes
// stripped so the message is readable in a log file.
func failure(subcommand string, loc string, res runner.Result) error {
	msg := strings.TrimSpace(StripANSI(res.Stderr))
	if msg == "" {
		msg = strings.TrimSpace(StripANSI(res.Stdout))
	}
	return fmt.Errorf("rclone %s %s failed (exit %d): %s", subcommand, loc, res.ExitCode, msg)
}

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

// TransferOptions are the filter and reporting flags shared by copy, sync and
// bisync.
//
// Filters are applied in rclone's argv order: includes, then --include-from,
// then excludes, then --exclude-from, then filters, then --filters-file. rclone
// filter rules are order-sensitive, so this ordering is part of the behaviour
// and matches the Python's _rclone_cmd_helper exactly.
type TransferOptions struct {
	Include     []string
	Exclude     []string
	Filter      []string
	IncludeFile string
	ExcludeFile string
	FiltersFile string
	DryRun      bool
	Progress    bool
}

// SyncOptions are TransferOptions plus sync's backup directory.
type SyncOptions struct {
	TransferOptions
	// BackupPath receives files sync would otherwise delete or overwrite. It is
	// an rclone destination, so it may be "remote:path".
	BackupPath string
}

// BisyncOptions are TransferOptions plus bisync's two recovery flags.
type BisyncOptions struct {
	TransferOptions
	Resync bool
	Force  bool
}

// CopytoOptions are the flags copyto takes. copyto deliberately gets neither
// --links nor --fast-list: it copies one object to one exact name.
type CopytoOptions struct {
	DryRun   bool
	Progress bool
}

// LsjsonOptions are the flags lsjson takes.
type LsjsonOptions struct {
	DirsOnly  bool
	FilesOnly bool
	Recursive bool
	// MaxDepth limits recursion. Zero means no --max-depth flag at all.
	MaxDepth int
	Filter   []string
}

// ---------------------------------------------------------------------------
// Argv builders
//
// These are pure functions of the client and the options, and they are the
// units the Python's 1238-line command-builder test suite exercises through its
// return_command=True paths. Keeping them separate from the running is what
// makes that suite portable.
// ---------------------------------------------------------------------------

// transferArgs builds copy/sync/bisync, whose flag layout is identical.
//
// --fast-list is always passed. The Python takes a use_fast_list parameter that
// no call site ever sets to False; rather than carry a knob whose zero value in
// Go would silently invert the Python default, the flag is fixed here.
func (c *Client) transferArgs(subcommand string, src, dst Location, o TransferOptions) []string {
	argv := []string{
		c.Binary, subcommand,
		"--config", c.ConfigPath,
		"--links",
		src.Spec(), dst.Spec(),
	}
	if o.DryRun {
		argv = append(argv, "--dry-run")
	}
	argv = append(argv, "--fast-list")
	for _, f := range o.Include {
		argv = append(argv, "--include", f)
	}
	if o.IncludeFile != "" {
		argv = append(argv, "--include-from", o.IncludeFile)
	}
	for _, f := range o.Exclude {
		argv = append(argv, "--exclude", f)
	}
	if o.ExcludeFile != "" {
		argv = append(argv, "--exclude-from", o.ExcludeFile)
	}
	for _, f := range o.Filter {
		argv = append(argv, "--filter", f)
	}
	if o.FiltersFile != "" {
		argv = append(argv, "--filters-file", o.FiltersFile)
	}
	if o.Progress {
		argv = append(argv, "--progress")
	}
	return argv
}

// CopyArgs builds `rclone copy`.
func (c *Client) CopyArgs(src, dst Location, o TransferOptions) []string {
	return c.transferArgs("copy", src, dst, o)
}

// SyncArgs builds `rclone sync`.
func (c *Client) SyncArgs(src, dst Location, o SyncOptions) []string {
	argv := c.transferArgs("sync", src, dst, o.TransferOptions)
	if o.BackupPath != "" {
		argv = append(argv, "--backup-dir", o.BackupPath)
	}
	return argv
}

// BisyncArgs builds `rclone bisync`.
func (c *Client) BisyncArgs(src, dst Location, o BisyncOptions) []string {
	argv := c.transferArgs("bisync", src, dst, o.TransferOptions)
	if o.Resync {
		argv = append(argv, "--resync")
	}
	if o.Force {
		argv = append(argv, "--force")
	}
	return argv
}

// CopytoArgs builds `rclone copyto`, which copies one object to one exact
// destination name.
//
// NOTE: the Python's rclone_copyto accepts a dry_run parameter and then never
// emits --dry-run, so a caller asking for a dry run would silently WRITE. No
// current call site passes True, so nothing is broken today, but the parameter
// is a loaded gun. It is implemented properly here.
func (c *Client) CopytoArgs(src, dst Location, o CopytoOptions) []string {
	argv := []string{c.Binary, "copyto", "--config", c.ConfigPath, src.Spec(), dst.Spec()}
	if o.DryRun {
		argv = append(argv, "--dry-run")
	}
	if o.Progress {
		argv = append(argv, "--progress")
	}
	return argv
}

// MkdirArgs builds `rclone mkdir`.
func (c *Client) MkdirArgs(loc Location) []string {
	return []string{c.Binary, "mkdir", "--config", c.ConfigPath, loc.Spec()}
}

// LsjsonArgs builds `rclone lsjson`.
func (c *Client) LsjsonArgs(loc Location, o LsjsonOptions) []string {
	argv := []string{c.Binary, "lsjson", "--config", c.ConfigPath, loc.Spec()}
	if o.DirsOnly {
		argv = append(argv, "--dirs-only")
	}
	if o.FilesOnly {
		argv = append(argv, "--files-only")
	}
	if o.Recursive {
		argv = append(argv, "--recursive")
	}
	// Symlinks are always followed as links. Every boxyard call site relies on
	// this: a box may contain symlinks and the listing has to describe them the
	// same way a transfer will move them.
	argv = append(argv, "--links")
	if o.MaxDepth != 0 {
		argv = append(argv, "--max-depth", strconv.Itoa(o.MaxDepth))
	}
	argv = append(argv, "--fast-list")
	for _, f := range o.Filter {
		argv = append(argv, "--filter", f)
	}
	return argv
}

// PurgeArgs builds `rclone purge`, which removes a directory and its contents.
func (c *Client) PurgeArgs(loc Location) []string {
	return []string{c.Binary, "purge", "--config", c.ConfigPath, loc.Spec()}
}

// CatArgs builds `rclone cat`.
func (c *Client) CatArgs(loc Location) []string {
	return []string{c.Binary, "cat", "--config", c.ConfigPath, loc.Spec()}
}

// MoveArgs builds `rclone move`, which moves the CONTENTS of source into dest.
func (c *Client) MoveArgs(src, dst Location) []string {
	return []string{c.Binary, "move", "--config", c.ConfigPath, src.Spec(), dst.Spec()}
}

// MovetoArgs builds `rclone moveto`, which renames source to the exact dest
// path.
func (c *Client) MovetoArgs(src, dst Location) []string {
	return []string{c.Binary, "moveto", "--config", c.ConfigPath, src.Spec(), dst.Spec()}
}

// DeleteFileArgs builds `rclone deletefile`, which removes a single object.
func (c *Client) DeleteFileArgs(loc Location) []string {
	return []string{c.Binary, "deletefile", "--config", c.ConfigPath, loc.Spec()}
}

// ---------------------------------------------------------------------------
// ANSI
// ---------------------------------------------------------------------------

// ansiEscape matches an ANSI escape sequence: a 7-bit C1 sequence, or a CSI
// sequence with its parameter, intermediate and final bytes.
//
// Ported from the Python (which took it from https://stackoverflow.com/a,
// Martijn Pieters et al., CC BY-SA 4.0).
var ansiEscape = regexp.MustCompile(`\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])`)

// StripANSI removes ANSI escape sequences from text.
//
// rclone colours its output when it thinks a terminal is attached, and bisync's
// result is classified by matching literal strings in stderr. Without this,
// a coloured "ERROR : Bisync aborted" would be classified as an unknown error
// and the box would never be resynced.
func StripANSI(text string) string { return ansiEscape.ReplaceAllString(text, "") }

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

// Copy copies source into dest, adding and updating but never deleting.
//
// The returned error covers only failures to run rclone at all (see
// internal/runner); rclone's own verdict is Output.OK.
func (c *Client) Copy(ctx context.Context, src, dst Location, o TransferOptions) (Output, error) {
	res, err := c.exec(ctx, c.CopyArgs(src, dst, o), 0)
	if err != nil {
		return Output{}, err
	}
	return outputOf(res), nil
}

// Copyto copies one object to one exact destination name.
func (c *Client) Copyto(ctx context.Context, src, dst Location, o CopytoOptions) (Output, error) {
	res, err := c.exec(ctx, c.CopytoArgs(src, dst, o), 0)
	if err != nil {
		return Output{}, err
	}
	return outputOf(res), nil
}

// Sync makes dest identical to source, DELETING anything in dest that is not in
// source. Set SyncOptions.BackupPath so that what it removes is recoverable.
func (c *Client) Sync(ctx context.Context, src, dst Location, o SyncOptions) (Output, error) {
	res, err := c.exec(ctx, c.SyncArgs(src, dst, o), 0)
	if err != nil {
		return Output{}, err
	}
	return outputOf(res), nil
}

// BisyncResult classifies the outcome of a bisync run. The string values are
// the Python enum's values.
type BisyncResult string

const (
	BisyncSuccess         BisyncResult = "success"
	BisyncConflicts       BisyncResult = "conflicts"
	BisyncNeedsResync     BisyncResult = "needs_resync"
	BisyncAllFilesChanged BisyncResult = "all_files_changed"
	BisyncOtherError      BisyncResult = "other_error"
)

// AllBisyncResults lists every BisyncResult, so an exhaustive switch has
// something to check itself against.
var AllBisyncResults = []BisyncResult{
	BisyncSuccess, BisyncConflicts, BisyncNeedsResync, BisyncAllFilesChanged, BisyncOtherError,
}

// Markers rclone prints that bisync's classification depends on.
const (
	bisyncNeedsResyncMarker     = "ERROR : Bisync aborted. Must run --resync to recover."
	bisyncAllFilesChangedMarker = "ERROR : Safety abort: all files were changed"
	bisyncConflictsMarker       = "NOTICE: - WARNING  New or changed in both paths"
)

// Bisync syncs both directions and classifies the outcome.
//
// The classification order matters and matches the Python: the two recoverable
// aborts are recognised first, then any other non-zero exit is an error, and
// only a run that exited zero can be reported as CONFLICTS or SUCCESS.
func (c *Client) Bisync(ctx context.Context, src, dst Location, o BisyncOptions) (BisyncResult, Output, error) {
	res, err := c.exec(ctx, c.BisyncArgs(src, dst, o), 0)
	if err != nil {
		return "", Output{}, err
	}
	out := outputOf(res)
	stderr := StripANSI(res.Stderr)

	switch {
	case strings.Contains(stderr, bisyncNeedsResyncMarker):
		return BisyncNeedsResync, out, nil
	case strings.Contains(stderr, bisyncAllFilesChangedMarker):
		return BisyncAllFilesChanged, out, nil
	case !res.OK():
		return BisyncOtherError, out, nil
	case strings.Contains(stderr, bisyncConflictsMarker):
		return BisyncConflicts, out, nil
	default:
		return BisyncSuccess, out, nil
	}
}

// Mkdir creates a directory, and any missing parents. It succeeds if the
// directory already exists.
//
// A failure here is always an error: every caller creates a directory it is
// about to write into, so carrying on without it would corrupt the write.
func (c *Client) Mkdir(ctx context.Context, loc Location) error {
	res, err := c.exec(ctx, c.MkdirArgs(loc), ListingTimeout)
	if err != nil {
		return err
	}
	if !res.OK() {
		return failure("mkdir", loc.Spec(), res)
	}
	return nil
}

// Entry is one item from an lsjson listing.
//
// This is rclone's output, not a boxyard model, so it is decoded with plain
// encoding/json rather than internal/strict: rclone emits many more fields than
// boxyard reads (hashes, tiers, encryption, backend metadata) and new rclone
// versions add more. Rejecting unknown fields here would make a routine rclone
// upgrade break every listing.
type Entry struct {
	// Path is relative to the listed directory; with Recursive it contains
	// slashes.
	Path string `json:"Path"`
	// Name is the final component.
	Name string `json:"Name"`
	Size int64  `json:"Size"`
	// ModTime is kept as rclone's raw RFC3339 string.
	ModTime string `json:"ModTime"`
	IsDir   bool   `json:"IsDir"`
}

// Lsjson lists loc.
//
// found reports whether the path exists: an absent path is a legitimate answer
// (it is how PathExists works), and is the ONLY non-zero exit that is not an
// error. Note that rclone prints a partial "[" on stdout before failing this
// way, so the exit code — not the output — is what decides.
func (c *Client) Lsjson(ctx context.Context, loc Location, o LsjsonOptions) (entries []Entry, found bool, err error) {
	res, execErr := c.exec(ctx, c.LsjsonArgs(loc, o), ListingTimeout)
	if execErr != nil {
		return nil, false, execErr
	}
	if res.ExitCode == exitDirNotFound || res.ExitCode == exitFileNotFound {
		return nil, false, nil
	}
	if !res.OK() {
		return nil, false, failure("lsjson", loc.Spec(), res)
	}
	if err := json.Unmarshal([]byte(res.Stdout), &entries); err != nil {
		return nil, false, fmt.Errorf("rclone lsjson %s returned unparseable JSON: %w", loc.Spec(), err)
	}
	return entries, true, nil
}

// PathExists reports whether loc exists and whether it is a directory.
//
// It works by listing the PARENT directory, because rclone has no stat: a
// listing of the path itself cannot distinguish an empty directory from a
// missing one.
func (c *Client) PathExists(ctx context.Context, loc Location) (exists, isDir bool, err error) {
	// The root of a remote always exists; there is no parent to list.
	if posixString(loc.Path) == "." {
		return true, true, nil
	}

	parts := posixParts(loc.Path)
	parent := ""
	if len(parts) > 1 {
		parent = posixJoin(parts[:len(parts)-1])
	}
	name := posixName(parts)

	entries, found, err := c.Lsjson(ctx, Location{Remote: loc.Remote, Path: parent}, LsjsonOptions{})
	if err != nil {
		return false, false, err
	}
	if !found {
		// The parent is not there, so neither is the child.
		return false, false, nil
	}
	for _, e := range entries {
		if e.Name == name {
			return true, e.IsDir, nil
		}
	}
	return false, false, nil
}

// Purge removes a directory and everything in it.
func (c *Client) Purge(ctx context.Context, loc Location) (Output, error) {
	res, err := c.exec(ctx, c.PurgeArgs(loc), 0)
	if err != nil {
		return Output{}, err
	}
	return outputOf(res), nil
}

// Cat reads a remote file.
//
// exists is false when the file is not there, which is a legitimate answer —
// sync records and tombstones are routinely absent. Any other failure is an
// error, so an unreachable remote is never mistaken for an empty one.
func (c *Client) Cat(ctx context.Context, loc Location) (exists bool, content string, err error) {
	res, execErr := c.exec(ctx, c.CatArgs(loc), ListingTimeout)
	if execErr != nil {
		return false, "", execErr
	}
	if res.ExitCode == exitDirNotFound || res.ExitCode == exitFileNotFound {
		return false, "", nil
	}
	if !res.OK() {
		return false, "", failure("cat", loc.Spec(), res)
	}
	return true, res.Stdout, nil
}

// Move moves the CONTENTS of src into dst, leaving src an empty directory.
func (c *Client) Move(ctx context.Context, src, dst Location) (Output, error) {
	res, err := c.exec(ctx, c.MoveArgs(src, dst), 0)
	if err != nil {
		return Output{}, err
	}
	return outputOf(res), nil
}

// Moveto renames src to exactly dst. Unlike Move it does not descend into the
// source; it is the rename primitive.
func (c *Client) Moveto(ctx context.Context, src, dst Location) (Output, error) {
	res, err := c.exec(ctx, c.MovetoArgs(src, dst), 0)
	if err != nil {
		return Output{}, err
	}
	return outputOf(res), nil
}

// Write writes content to a remote file, creating parent directories as needed.
//
// rclone has no "write this string" verb, so the content goes to a local
// temporary file which is then copied to its exact destination name.
func (c *Client) Write(ctx context.Context, dst Location, content string) (Output, error) {
	tmp, err := os.CreateTemp("", "boxyard-*.tmp")
	if err != nil {
		return Output{}, fmt.Errorf("cannot stage a temporary file for writing to %s: %w", dst.Spec(), err)
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)

	if _, err := tmp.WriteString(content); err != nil {
		tmp.Close()
		return Output{}, fmt.Errorf("cannot write the staging file for %s: %w", dst.Spec(), err)
	}
	if err := tmp.Close(); err != nil {
		return Output{}, fmt.Errorf("cannot close the staging file for %s: %w", dst.Spec(), err)
	}

	return c.Copyto(ctx, Local(tmpPath), dst, CopytoOptions{})
}

// Delete removes a single remote file.
func (c *Client) Delete(ctx context.Context, loc Location) (Output, error) {
	res, err := c.exec(ctx, c.DeleteFileArgs(loc), 0)
	if err != nil {
		return Output{}, err
	}
	return outputOf(res), nil
}

// ---------------------------------------------------------------------------
// POSIX path helpers
//
// These mirror pathlib.PurePosixPath's parts / parent / name, which is what the
// Python uses to split a remote path. path.Clean is deliberately NOT used: it
// collapses "..", where pathlib leaves it alone, and the two implementations
// must split identically.
// ---------------------------------------------------------------------------

// posixParts splits p the way PurePosixPath.parts does: a leading "/" is its
// own part, empty and "." components are dropped, and nothing else is
// normalised.
func posixParts(p string) []string {
	var parts []string
	if strings.HasPrefix(p, "/") {
		parts = append(parts, "/")
	}
	for _, seg := range strings.Split(p, "/") {
		if seg == "" || seg == "." {
			continue
		}
		parts = append(parts, seg)
	}
	return parts
}

// posixJoin reassembles parts produced by posixParts.
func posixJoin(parts []string) string {
	if len(parts) == 0 {
		return ""
	}
	if parts[0] == "/" {
		return "/" + strings.Join(parts[1:], "/")
	}
	return strings.Join(parts, "/")
}

// posixName returns the final component the way PurePosixPath.name does, which
// is empty for the root.
func posixName(parts []string) string {
	if len(parts) == 0 {
		return ""
	}
	last := parts[len(parts)-1]
	if last == "/" {
		return ""
	}
	return last
}

// posixString renders p the way PurePosixPath.as_posix does, which turns "" and
// "." alike into ".". PathExists relies on that: a Location whose path is empty
// IS the root of the remote.
func posixString(p string) string {
	joined := posixJoin(posixParts(p))
	if joined == "" {
		return "."
	}
	return joined
}

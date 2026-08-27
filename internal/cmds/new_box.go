package cmds

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/locking"
	"github.com/lukastk/boxyard/internal/models"
	"github.com/lukastk/boxyard/internal/naming"
	"github.com/lukastk/boxyard/internal/sysinfo"
)

// NewBoxOptions mirrors the Python `new_box` signature.
type NewBoxOptions struct {
	// StorageLocation defaults to the config's default_storage_location.
	StorageLocation string
	BoxName         string
	// FromPath moves an existing directory into the yard as the new box's
	// DATA. Mutually exclusive with GitCloneURL.
	FromPath string
	// CopyFromPath copies FromPath instead of moving it.
	CopyFromPath bool
	// CreatorHostname defaults to this machine's hostname.
	CreatorHostname string
	// CreationTimestampUTC overrides the creation timestamp. Nil means now.
	CreationTimestampUTC *time.Time
	// InitialiseGit runs `git init` in the new box's DATA.
	InitialiseGit bool
	// Claim makes this machine the box's write owner. Nil means the default,
	// which is to claim — a pointer so a caller can say "no" without the zero
	// value silently meaning it.
	Claim *bool
	// GitCloneURL clones a repository as the new box's DATA.
	GitCloneURL string
	Verbose     bool
	// Out receives the progress messages Python prints under Verbose. A nil
	// Out means silent.
	Out io.Writer
}

// NewBox creates a box and returns its index name.
//
// Ported from pts/mod/cmds/01_new_box.pct.py.
//
// s is only used when `sync_before_new_box` is on, to fetch every remote
// boxmeta before an id is minted; callers that never enable the setting may
// pass nil, and NewBox refuses loudly rather than silently minting an id
// without the check.
func NewBox(ctx context.Context, cfg *config.Config, s MetaSyncStore, opts NewBoxOptions) (string, error) {
	storageLocation := opts.StorageLocation
	if storageLocation == "" {
		storageLocation = cfg.DefaultStorageLocation
	}
	if _, ok := cfg.StorageLocations[storageLocation]; !ok {
		names := make([]string, 0, len(cfg.StorageLocations))
		for name := range cfg.StorageLocations {
			names = append(names, name)
		}
		sort.Strings(names)
		return "", fmt.Errorf("Invalid storage location: %s. Must be one of: %s.",
			storageLocation, strings.Join(names, ", "))
	}

	if opts.GitCloneURL != "" && opts.FromPath != "" {
		return "", errors.New("`git_clone_url` and `from_path` are mutually exclusive.")
	}
	if opts.BoxName == "" && opts.FromPath == "" && opts.GitCloneURL == "" {
		return "", errors.New("Either `box_name`, `from_path`, or `git_clone_url` must be provided.")
	}

	fromPath := opts.FromPath
	if fromPath != "" {
		expanded, err := config.ExpandUser(fromPath)
		if err != nil {
			return "", err
		}
		// Python resolves the path (following symlinks) before using it, so a
		// symlinked source is moved by its real path, not by the link.
		resolved, err := filepath.EvalSymlinks(expanded)
		if err != nil {
			return "", fmt.Errorf("cannot resolve --from path %q: %w", opts.FromPath, err)
		}
		fromPath, err = filepath.Abs(resolved)
		if err != nil {
			return "", err
		}
	}

	boxName := opts.BoxName
	if boxName == "" && fromPath != "" {
		boxName = filepath.Base(fromPath)
	}
	if boxName == "" && opts.GitCloneURL != "" {
		boxName = ExtractBoxNameFromGitURL(opts.GitCloneURL)
	}

	// The name is used verbatim as a directory name, so reject anything that is
	// not a single path component before any of the box is created.
	if err := naming.ValidateBoxName(boxName); err != nil {
		return "", err
	}

	if fromPath == "" && opts.CopyFromPath {
		return "", errors.New("`from_path` must be provided if `copy_from_path` is True.")
	}

	creatorHostname := opts.CreatorHostname
	if creatorHostname == "" {
		creatorHostname = sysinfo.Hostname()
	}

	// `sync_before_new_box` syncs every boxmeta first, so that a box id already
	// taken on the remote is seen before this machine mints a colliding one.
	// Skipping it silently would remove the very guarantee the setting exists
	// to provide, so a caller that turned it on without wiring a store is a
	// programming error, not a fallback.
	if cfg.SyncBeforeNewBox {
		if s == nil {
			return "", fmt.Errorf("`sync_before_new_box` is enabled but this call site passed no store, so the pre-flight boxmeta sync cannot run")
		}
		if opts.Verbose && opts.Out != nil {
			fmt.Fprintln(opts.Out, "Syncing boxmetas before creating new box...")
		}
		if err := SyncMissingBoxMetas(ctx, cfg, s, SyncMissingBoxMetasOptions{
			Verbose: opts.Verbose,
			Out:     opts.Out,
		}); err != nil {
			return "", err
		}
	}

	printf := func(format string, a ...any) {
		if opts.Out != nil && opts.Verbose {
			fmt.Fprintf(opts.Out, format, a...)
		}
	}

	boxyardMeta, err := models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return "", err
	}

	if fromPath != "" {
		// Moving a box's own DATA directory into a new box would deregister the
		// old box by taking its directory away.
		for _, bm := range boxyardMeta.BoxMetas {
			dataPath, err := bm.LocalPartPath(cfg, enums.PartData)
			if err != nil {
				return "", err
			}
			if dataPath == fromPath && !opts.CopyFromPath {
				return "", fmt.Errorf("'%s' is already a boxyard box. Use `copy_from_path=True` to copy the contents of this box into a new box.", fromPath)
			}
		}
	}

	mgr := locking.NewManager(cfg.BoxyardDataPath)
	release, err := mgr.GlobalLock(locking.GlobalLockTimeout)
	if err != nil {
		return "", err
	}
	// Everything from here on runs under the lock. The release is deferred
	// rather than called at each exit, because Python's release sat outside its
	// try/except: a failure in the meta refresh left the lock held for the rest
	// of the process.
	defer release()

	// Re-read the yard now that the lock is held: the snapshot above was taken
	// before it, so a box created concurrently would be missing from the
	// collision check the lock exists to make meaningful.
	boxyardMeta, err = models.GetBoxyardMeta(cfg, false)
	if err != nil {
		return "", err
	}
	existingIDs := make(map[string]bool, len(boxyardMeta.BoxMetas))
	for _, bm := range boxyardMeta.BoxMetas {
		existingIDs[bm.BoxID()] = true
	}

	// A caller-supplied timestamp is resolved BEFORE the id is generated, so
	// the collision check runs against the id that will actually be written.
	fixedTimestamp := ""
	if opts.CreationTimestampUTC != nil {
		fixedTimestamp, err = models.FormatCreationTimestamp(cfg, opts.CreationTimestampUTC.UTC())
		if err != nil {
			return "", err
		}
	}
	creationTimestamp, boxSubid, err := models.GenerateUniqueBoxID(cfg, existingIDs, fixedTimestamp)
	if err != nil {
		return "", err
	}

	// Claim the box for this machine as it is created.
	//
	// The machine creating a box is the machine that will work on it — that is
	// what creating one MEANS — and a box nobody has claimed is a box two
	// machines can still diverge on, which is the problem ownership exists to
	// remove. `include` already nudges about exactly this; creation is a
	// stronger signal than inclusion and was saying nothing at all.
	//
	// The "unowned by default" rule was a MIGRATION guarantee: v0.5.2 promised
	// that "nothing changes for the 583 boxes in the yard until someone claims
	// them". A box created afterwards has no v0.4.x behaviour to preserve.
	//
	// Set locally, with NO remote call, which is what keeps `boxyard new`
	// offline. ClaimBox reads the remote back to verify because two machines
	// can claim the same EXISTING box at once; a box created a moment ago
	// cannot be contested, because no other machine knows its id yet.
	claim := opts.Claim == nil || *opts.Claim
	writeOwner := ""
	if claim {
		if cfg.MachineName != "" {
			writeOwner = cfg.MachineName
		} else {
			// Not fatal: a machine with no machine_name is an expected state,
			// and refusing to create a box over an ownership setting would be
			// out of proportion. Said out loud rather than skipped silently,
			// and doctor reports both the missing name and the unowned box.
			// Unconditional (printf is gated on --verbose, which the CLI never
			// sets, and a notice nobody sees is the silent skip it exists to
			// avoid) but on STDERR: `boxyard new` prints the index name on
			// stdout and callers parse it — `cd $(boxyard new ...)` — so a
			// notice there is read as part of the answer.
			fmt.Fprintln(os.Stderr, "Created without a write owner: this machine has no "+
				"`machine_name` set, so it cannot claim a box. See `boxyard doctor`.")
		}
	}

	boxMeta := &models.BoxMeta{
		CreationTimestampUTC: creationTimestamp,
		BoxSubid:             boxSubid,
		Name:                 boxName,
		StorageLocation:      storageLocation,
		CreatorHostname:      creatorHostname,
		Groups:               append([]string{}, cfg.DefaultBoxGroups...),
		Parents:              []string{},
		WriteOwner:           writeOwner,
	}

	boxPath := boxMeta.LocalPath(cfg)
	boxDataPath, err := boxMeta.LocalPartPath(cfg, enums.PartData)
	if err != nil {
		return "", err
	}
	boxConfPath, err := boxMeta.LocalPartPath(cfg, enums.PartConf)
	if err != nil {
		return "", err
	}

	// Everything that puts the box on disk happens under one rollback: a box is
	// registered by the directories it occupies, so a partial creation would
	// leave a broken registration behind — and every retry would add another.
	movedFromPath := ""
	if err := func() error {
		if err := boxMeta.Save(cfg); err != nil {
			return err
		}
		if err := os.MkdirAll(boxPath, 0o755); err != nil {
			return err
		}
		if err := os.MkdirAll(boxConfPath, 0o755); err != nil {
			return err
		}

		switch {
		case opts.GitCloneURL != "":
			printf("Cloning %s\n", opts.GitCloneURL)
			if err := gitClone(opts.GitCloneURL, boxDataPath, opts.Verbose, opts.Out); err != nil {
				return err
			}
		case fromPath != "":
			if opts.CopyFromPath {
				if err := copyTree(fromPath, boxDataPath); err != nil {
					return err
				}
			} else {
				if err := movePath(fromPath, boxDataPath); err != nil {
					return err
				}
				movedFromPath = fromPath
			}
		default:
			if err := os.MkdirAll(boxDataPath, 0o755); err != nil {
				return err
			}
		}

		// Skip git init when cloning (already a git box) or when .git exists.
		if opts.InitialiseGit && opts.GitCloneURL == "" && !pathExists(filepath.Join(boxDataPath, ".git")) {
			printf("Initialising git box\n")
			gitInit(boxDataPath)
		}
		return nil
	}(); err != nil {
		if cleanupErr := rollbackNewBox(boxPath, boxDataPath, movedFromPath); cleanupErr != nil {
			fmt.Fprintf(os.Stderr,
				"Warning: failed to clean up the partially-created box '%s': %v\n",
				boxMeta.IndexName(), cleanupErr)
		}
		return "", err
	}

	if _, err := models.RefreshBoxyardMeta(cfg, true); err != nil {
		return "", err
	}
	return boxMeta.IndexName(), nil
}

var (
	gitSSHRe   = regexp.MustCompile(`^git@[^:]+:(.+)$`)
	gitHTTPSRe = regexp.MustCompile(`^https?://[^/]+/(.+)$`)
)

// ExtractBoxNameFromGitURL derives a box name from a git URL (SSH or HTTPS).
func ExtractBoxNameFromGitURL(url string) string {
	url = strings.TrimRight(url, "/")
	url = strings.TrimSuffix(url, ".git")

	if m := gitSSHRe.FindStringSubmatch(url); m != nil {
		return lastPathSegment(m[1])
	}
	if m := gitHTTPSRe.FindStringSubmatch(url); m != nil {
		return lastPathSegment(m[1])
	}
	return lastPathSegment(url)
}

func lastPathSegment(s string) string {
	parts := strings.Split(s, "/")
	return parts[len(parts)-1]
}

// gitClone clones into dest, reporting git's own diagnosis on failure. Which
// failure it was — a bad URL, no network, no permission — lives only in git's
// stderr, so it is captured rather than discarded.
func gitClone(url, dest string, verbose bool, out io.Writer) error {
	cmd := exec.Command("git", "clone", url, dest)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	if verbose && out != nil {
		cmd.Stdout = out
	}
	if err := cmd.Run(); err != nil {
		detail := strings.TrimSpace(stderr.String())
		msg := fmt.Sprintf("Failed to clone box from %s (%v)", url, err)
		if detail != "" {
			msg += ": " + detail
		}
		return errors.New(msg)
	}
	return nil
}

// gitInit initialises a repository, warning rather than failing.
//
// The box is fully created and registered by this point; `git init` is a
// convenience laid on top of it. Failing here would roll the box back and, with
// --from, unwind a directory move — too much to lose over an optional step. The
// warning goes to stderr unconditionally: the CLI passes verbose=false, so
// gating it on verbosity would make a failed `git init` completely silent.
func gitInit(dir string) {
	cmd := exec.Command("git", "init")
	cmd.Dir = dir
	var stderr strings.Builder
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		detail := strings.TrimSpace(stderr.String())
		msg := fmt.Sprintf("Warning: `git init` failed in '%s' (%v); the box was created without it", dir, err)
		if detail != "" {
			msg += ": " + detail
		}
		fmt.Fprintln(os.Stderr, msg)
	}
}

// rollbackNewBox undoes a partially-created box.
func rollbackNewBox(boxPath, boxDataPath, movedFromPath string) error {
	if movedFromPath != "" && pathExists(boxDataPath) {
		// `from_path` was moved into the box — put it back where it came from.
		if err := movePath(boxDataPath, movedFromPath); err != nil {
			return err
		}
	} else if pathExists(boxDataPath) {
		if err := os.RemoveAll(boxDataPath); err != nil {
			return err
		}
	}
	if pathExists(boxPath) {
		if err := os.RemoveAll(boxPath); err != nil {
			return err
		}
	}
	return nil
}

// copyTree copies src to dst, which must not exist — matching shutil.copytree.
//
// Symlinks are recreated as symlinks (copytree's default), file modes are
// preserved, and anything that is neither a regular file, a directory nor a
// symlink is a loud error rather than a silent omission.
// movePath moves a tree, ACROSS filesystems when it has to.
//
// os.Rename alone cannot cross a filesystem boundary, so staging a tree
// anywhere not on the same filesystem as user_boxes_path — /tmp where it is
// tmpfs, /dev/shm, an external disk — failed outright with EXDEV. The same
// command worked on mymain and failed on ideapad purely because of how each
// mounts /tmp.
//
// Rename first, so the fast path is unchanged and the common case is still one
// atomic syscall; copy-then-remove only when the kernel says the two ends are
// on different devices. Any OTHER rename error is returned as-is rather than
// papered over by a copy.
func movePath(src, dst string) error {
	err := os.Rename(src, dst)
	if err == nil {
		return nil
	}
	var linkErr *os.LinkError
	if !errors.As(err, &linkErr) || !errors.Is(linkErr.Err, syscall.EXDEV) {
		return err
	}
	if err := copyTree(src, dst); err != nil {
		return err
	}
	// Only after the copy is known good: removing first would risk the
	// source on a failure, and `--from` without `--copy` is the caller's
	// only copy of that data.
	return os.RemoveAll(src)
}

func copyTree(src, dst string) error {
	info, err := os.Lstat(src)
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return fmt.Errorf("%s is not a directory", src)
	}
	if pathExists(dst) {
		return fmt.Errorf("%s already exists", dst)
	}
	return copyTreeInto(src, dst, info.Mode().Perm())
}

func copyTreeInto(src, dst string, perm os.FileMode) error {
	if err := os.MkdirAll(dst, perm); err != nil {
		return err
	}
	entries, err := os.ReadDir(src)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		srcPath := filepath.Join(src, entry.Name())
		dstPath := filepath.Join(dst, entry.Name())
		info, err := entry.Info()
		if err != nil {
			return err
		}
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			target, err := os.Readlink(srcPath)
			if err != nil {
				return err
			}
			if err := os.Symlink(target, dstPath); err != nil {
				return err
			}
		case info.IsDir():
			if err := copyTreeInto(srcPath, dstPath, info.Mode().Perm()); err != nil {
				return err
			}
		case info.Mode().IsRegular():
			if err := copyFile(srcPath, dstPath, info.Mode().Perm()); err != nil {
				return err
			}
		default:
			return fmt.Errorf("cannot copy %s: unsupported file type %s", srcPath, info.Mode())
		}
	}
	return nil
}

func copyFile(src, dst string, perm os.FileMode) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, perm)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

func pathExists(p string) bool {
	_, err := os.Lstat(p)
	return err == nil
}

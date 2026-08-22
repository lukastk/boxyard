// Package symlinks materialises boxyard's group tree: the browsable directory
// of symlinks under `user_box_groups_path` (the user's ~/g), one directory per
// group, each holding one symlink per included box pointing at that box's DATA
// directory.
//
// Ported from create_user_box_group_symlinks in src/boxyard/_models.py.
//
// # SHAPE OF THE OPERATION
//
// Build is idempotent and works in four passes, in this order — the order is
// load-bearing:
//
//  1. Plan.    Compute every symlink that SHOULD exist.
//  2. Remove.  Unlink every symlink in the tree that is not in the plan.
//  3. Inspect. Refuse to continue if any REAL file is left in the tree.
//  4. Create.  Make the planned symlinks, then prune emptied directories.
//
// Inspect sits between Remove and Create deliberately: debris aborts the whole
// build before anything is created, so a tree with a stray real file in it is
// reported rather than half-rebuilt around. `boxyard doctor`'s
// "group-tree-debris" check exists to point at exactly this failure, and its
// hint promises that "create-user-symlinks refuses to run while real files are
// in the group tree" — so the refusal must stay loud.
//
// # THIS PACKAGE DELETES THINGS
//
// It runs against the user's real ~/g. Two invariants keep that safe, and both
// are enforced rather than assumed:
//
//   - Nothing that is not a symlink is ever unlinked. Directories are removed
//     only by os.Remove, which refuses a non-empty directory.
//   - Every path handed to a remove call is checked to be inside
//     user_box_groups_path first (assertUnder). That is structurally true of
//     every path this package constructs; the check is there so that a future
//     refactor that breaks it fails loudly instead of eating a home directory.
//
// The tree walks NEVER descend through a symlink. Every symlink here points at
// a box's DATA directory, so a walk that followed them would scan — and, worse,
// prune inside — the user's actual work.
package symlinks

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/config"
	"github.com/lukastk/boxyard/internal/enums"
	"github.com/lukastk/boxyard/internal/models"
)

// Build materialises the group symlink tree for the included boxes.
//
// Warnings go to os.Stdout, matching the Python's bare print(). Use BuildTo to
// capture them.
func Build(cfg *config.Config, meta *models.BoxyardMeta) error {
	return BuildTo(cfg, meta, os.Stdout)
}

// BuildTo is Build with warnings written to w.
func BuildTo(cfg *config.Config, meta *models.BoxyardMeta, w io.Writer) error {
	root := cfg.UserBoxGroupsPath
	if root == "" {
		return errors.New("config.user_box_groups_path is not set")
	}

	// Only boxes whose DATA is checked out on this machine get a symlink: the
	// tree is a view of what is actually here, not of the whole yard.
	included := make([]*models.BoxMeta, 0, len(meta.BoxMetas))
	for _, bm := range meta.BoxMetas {
		if bm.CheckIncluded(cfg) {
			included = append(included, bm)
		}
	}
	// Oldest-first. This decides which box wins a title collision (see
	// conflictTitle), so it is part of the contract, not a cosmetic ordering.
	models.SortByCreation(included)

	groups, err := mergeGroups(cfg, included, w)
	if err != nil {
		return err
	}

	plan, err := planSymlinks(cfg, groups, included)
	if err != nil {
		return err
	}
	wanted := make(map[string]bool, len(plan))
	for _, l := range plan {
		wanted[l.link] = true
	}

	if err := removeUnwantedSymlinks(root, wanted); err != nil {
		return err
	}
	if err := inspectForDebris(root, wanted); err != nil {
		return err
	}
	if err := createSymlinks(root, plan); err != nil {
		return err
	}
	return pruneEmptyNonGroupDirs(root, groups)
}

// --- groups ---------------------------------------------------------------

// groupSpec is one row of the merged group table: a real group, or a virtual
// group that has replaced one.
type groupSpec struct {
	// name is the group's name — what a box lists in its `groups` field, and
	// the key the prune pass compares directory paths against.
	name string
	// symlinkName is the directory the group's symlinks live in, relative to
	// user_box_groups_path. It may contain "/" ("active/all", "ctx/macbook"),
	// in which case the group nests into subdirectories.
	symlinkName string
	titleMode   config.BoxGroupTitleMode
	// virtual is nil for a real group. When set, membership is the filter
	// expression rather than the group name.
	virtual *config.VirtualBoxGroupConfig
}

func (g *groupSpec) contains(bm *models.BoxMeta) bool {
	if g.virtual != nil {
		return g.virtual.IsInGroup(bm.Groups)
	}
	return slices.Contains(bm.Groups, g.name)
}

// mergeGroups builds the effective group table: the configured groups, plus a
// default entry for every group a box claims that the config does not mention,
// with the virtual groups merged over the top.
//
// A virtual group whose name collides with a real one REPLACES it — symlink
// name, title mode and membership rule all come from the virtual group, and the
// real group's membership-by-name is lost. That is what the Python's
// `groups.update(virtual_box_groups)` does, and it is why the collision is
// warned about rather than silently accepted.
//
// ORDERING: the result is sorted by group name. The Python iterates a dict in
// insertion order (config file order, then discovery order), which Go's maps
// cannot reproduce and which no caller can rely on anyway. The order is only
// observable if two DISTINCT groups resolve to the same symlink_name and share
// a box, in which case both implementations pick arbitrarily; sorting at least
// makes Go's choice deterministic and reproducible.
func mergeGroups(cfg *config.Config, included []*models.BoxMeta, w io.Writer) ([]groupSpec, error) {
	real, virtual := models.GroupConfigs(cfg, included)

	names := make([]string, 0, len(virtual))
	for name := range virtual {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if _, clash := real[name]; clash {
			if _, err := fmt.Fprintf(w, "Warning: Virtual box group '%s' is also a regular box group.\n", name); err != nil {
				return nil, fmt.Errorf("cannot write warning: %w", err)
			}
		}
	}

	specs := make(map[string]groupSpec, len(real)+len(virtual))
	for name, g := range real {
		specs[name] = groupSpec{
			name:        name,
			symlinkName: orGroupName(g.SymlinkName, name),
			titleMode:   g.BoxTitleMode,
		}
	}
	for name, g := range virtual {
		specs[name] = groupSpec{
			name:        name,
			symlinkName: orGroupName(g.SymlinkName, name),
			titleMode:   g.BoxTitleMode,
			virtual:     g,
		}
	}

	out := make([]groupSpec, 0, len(specs))
	for _, s := range specs {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].name < out[j].name })
	return out, nil
}

// orGroupName mirrors the Python's `group_config.symlink_name or group_name`.
// An empty symlink_name falls back to the group name — in Python because "" is
// falsy, here explicitly. That is not a defensive fallback: an unset
// symlink_name is the normal, expected case.
func orGroupName(symlinkName, groupName string) string {
	if symlinkName == "" {
		return groupName
	}
	return symlinkName
}

// --- planning -------------------------------------------------------------

// link is one symlink to materialise: link -> dest.
type link struct {
	dest string // the box's DATA directory
	link string // where in the group tree the symlink goes
}

func planSymlinks(cfg *config.Config, groups []groupSpec, included []*models.BoxMeta) ([]link, error) {
	// The plan is an ordered slice, not a map, and may contain the same link
	// path twice — see conflictTitle. Later entries win, because createSymlinks
	// processes them in order and rewrites a symlink that points elsewhere.
	var plan []link
	for _, g := range groups {
		titleCounter := map[string]int{}
		for _, bm := range included {
			if !g.contains(bm) {
				continue
			}
			dest, err := bm.LocalPartPath(cfg, enums.PartData)
			if err != nil {
				return nil, err
			}
			title, err := boxTitle(bm, g.titleMode)
			if err != nil {
				return nil, fmt.Errorf("box group '%s': %w", g.name, err)
			}
			title = conflictTitle(titleCounter, title)
			plan = append(plan, link{
				dest: dest,
				link: filepath.Join(cfg.UserBoxGroupsPath, filepath.FromSlash(g.symlinkName), title),
			})
		}
	}
	return plan, nil
}

// boxTitle is the name the box takes inside a group's directory.
func boxTitle(bm *models.BoxMeta, mode config.BoxGroupTitleMode) (string, error) {
	switch mode {
	case config.TitleIndexName:
		return bm.IndexName(), nil
	case config.TitleDatetimeAndName:
		return bm.CreationTimestampUTC + "__" + bm.Name, nil
	case config.TitleName:
		return bm.Name, nil
	default:
		return "", fmt.Errorf("Invalid box title mode: %s", mode)
	}
}

// conflictTitle reproduces the Python's title de-duplication, INCLUDING its
// bugs. It is deliberately not fixed here; see the report accompanying this
// port and the `# TODO` on the Python line.
//
// The Python is:
//
//	if title_counter[title] > 1:
//	    title = f"{title} (CONFLICT {title_counter[title]})"
//	title_counter[title] += 1
//
// Two things are wrong with it, and they compound:
//
//   - The threshold is `> 1` where it should be `> 0`. The counter holds how
//     many boxes have ALREADY taken this title, so the SECOND box to want
//     "foo" sees a count of 1, does not trigger, and takes "foo" as well.
//   - The increment is applied to the possibly-REWRITTEN title, not to the
//     original. Once a box is renamed to "foo (CONFLICT 2)", the count for
//     "foo" stops rising, so it is pinned at 2 forever and every later box
//     computes the identical suffix.
//
// The result for N boxes all wanting the title "foo", in oldest-first order:
//
//	box 1 -> "foo"
//	box 2 -> "foo"                (collides with box 1)
//	box 3 -> "foo (CONFLICT 2)"
//	box 4 -> "foo (CONFLICT 2)"   (collides with box 3)
//	box N -> "foo (CONFLICT 2)"   (... and so does every one after it)
//
// So the mechanism never actually disambiguates anything: it produces at most
// two distinct names, and createSymlinks then resolves each collision by
// last-one-wins, silently dropping the older box from the group. The intended
// behaviour is almost certainly `> 0` plus incrementing the original title,
// which yields "foo", "foo (CONFLICT 1)", "foo (CONFLICT 2)", ...
func conflictTitle(counter map[string]int, title string) string {
	if counter[title] > 1 {
		title = fmt.Sprintf("%s (CONFLICT %d)", title, counter[title])
	}
	counter[title]++
	return title
}

// --- removal --------------------------------------------------------------

// removeUnwantedSymlinks unlinks every symlink in the tree that the plan does
// not call for. Real files and directories are left completely alone; the next
// pass decides what to do about them.
func removeUnwantedSymlinks(root string, wanted map[string]bool) error {
	// The whole tree is collected before anything is unlinked, rather than
	// mutating it mid-walk.
	var found []string
	if err := collectTree(root, &found); err != nil {
		return err
	}
	for _, p := range found {
		if wanted[p] {
			continue
		}
		fi, err := os.Lstat(p)
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				// Removed by someone else between the walk and now.
				continue
			}
			return fmt.Errorf("cannot inspect '%s': %w", p, err)
		}
		if fi.Mode()&fs.ModeSymlink == 0 {
			continue
		}
		if err := assertUnder(root, p); err != nil {
			return err
		}
		if err := os.Remove(p); err != nil {
			return fmt.Errorf("cannot remove stale group symlink '%s': %w", p, err)
		}
	}
	return nil
}

// collectTree appends every entry beneath root, hidden entries included, and
// NEVER descends through a symlink.
//
// This is the equivalent of the Python's Path.glob("**/*"), whose behaviour was
// verified empirically rather than assumed: it DOES yield dotfiles and descends
// into hidden directories, and it does NOT recurse into symlinked directories.
func collectTree(root string, out *[]string) error {
	entries, err := os.ReadDir(root)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			// The group tree does not exist yet. Not an error: it is created
			// lazily, only if there is something to put in it.
			return nil
		}
		return fmt.Errorf("cannot read group tree directory '%s': %w", root, err)
	}
	for _, e := range entries {
		p := filepath.Join(root, e.Name())
		*out = append(*out, p)
		// DirEntry.Type reports the LINK's own type, so a symlink to a
		// directory has IsDir() == false here. Both checks are kept: this walk
		// following a symlink would mean walking into a box.
		if e.Type()&fs.ModeSymlink != 0 || !e.IsDir() {
			continue
		}
		if err := collectTree(p, out); err != nil {
			return err
		}
	}
	return nil
}

// --- debris ---------------------------------------------------------------

// inspectForDebris fails if anything that is not a symlink or a directory is
// left in the group tree. The tree is entirely generated, so a real file in it
// is either something the user misplaced or a sign that a box was copied in
// rather than linked — either way it must not be deleted, and building around
// it would hide it.
//
// Hidden entries are exempt, matching the Python: editors and syncing tools
// leave .DS_Store and friends about, and those are not worth aborting over.
func inspectForDebris(root string, wanted map[string]bool) error {
	entries, err := os.ReadDir(root)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("cannot read group tree directory '%s': %w", root, err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".") {
			continue
		}
		p := filepath.Join(root, e.Name())
		// Stat, not Lstat: Python's is_dir() follows symlinks, so a symlink to
		// a directory counts as a directory here.
		if isDir(p) {
			if err := inspectFolder(root, p, wanted); err != nil {
				return err
			}
			continue
		}
		return fmt.Errorf("'%s' is in the user box group path '%s' but is not a directory!", p, root)
	}
	return nil
}

func inspectFolder(root, dir string, wanted map[string]bool) error {
	if isSymlink(dir) {
		// Never look inside a symlink: it points at a box.
		return nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("cannot read group directory '%s': %w", dir, err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".") {
			continue
		}
		p := filepath.Join(dir, e.Name())
		if isDir(p) {
			if err := inspectFolder(root, p, wanted); err != nil {
				return err
			}
			continue
		}
		// Anything left is a real file or a symlink to one. A symlink can only
		// still be here if the plan wants it, since removeUnwantedSymlinks has
		// already run.
		if !wanted[p] {
			return fmt.Errorf("File '%s' is in the user box group path '%s'.", p, root)
		}
	}
	return nil
}

// --- creation -------------------------------------------------------------

func createSymlinks(root string, plan []link) error {
	for _, l := range plan {
		if err := os.MkdirAll(filepath.Dir(l.link), 0o755); err != nil {
			return fmt.Errorf("cannot create group directory '%s': %w", filepath.Dir(l.link), err)
		}
		fi, err := os.Lstat(l.link)
		switch {
		case err == nil && fi.Mode()&fs.ModeSymlink != 0:
			if sameTarget(l.link, l.dest) {
				continue // Already correct; leave it alone.
			}
			if err := assertUnder(root, l.link); err != nil {
				return err
			}
			if err := os.Remove(l.link); err != nil {
				return fmt.Errorf("cannot replace group symlink '%s': %w", l.link, err)
			}
		case err == nil:
			// A real file or directory is sitting where a symlink belongs.
			return fmt.Errorf("'%s' is in the user box group path '%s' but is not a symlink!", l.link, root)
		case !errors.Is(err, fs.ErrNotExist):
			return fmt.Errorf("cannot inspect '%s': %w", l.link, err)
		}
		if err := os.Symlink(l.dest, l.link); err != nil {
			return fmt.Errorf("cannot create group symlink '%s' -> '%s': %w", l.link, l.dest, err)
		}
	}
	return nil
}

// sameTarget reports whether the existing symlink already points at dest.
//
// The exact-target check is the fast path and covers every symlink this package
// wrote. The resolved comparison behind it is the Python's
// `symlink_path.resolve() != dest_path.resolve()`, which also treats a link
// reaching dest by another route (a symlinked parent, /tmp vs /private/tmp on
// macOS) as already correct.
func sameTarget(linkPath, dest string) bool {
	target, err := os.Readlink(linkPath)
	if err != nil {
		return false
	}
	if target == dest {
		return true
	}
	return resolveLenient(linkPath) == resolveLenient(dest)
}

// resolveLenient mimics pathlib.Path.resolve(strict=False): follow the symlink
// chain at p, then canonicalise the longest existing prefix of the result,
// leaving any non-existent tail as-is. A broken symlink therefore resolves to
// what it points AT, which is how a link to a box whose data has gone away
// still compares equal to that box's data path.
func resolveLenient(p string) string {
	abs, err := filepath.Abs(p)
	if err != nil {
		abs = filepath.Clean(p)
	}
	// Bounded, so a symlink cycle terminates instead of hanging.
	for i := 0; i < 40; i++ {
		target, err := os.Readlink(abs)
		if err != nil {
			break
		}
		if !filepath.IsAbs(target) {
			target = filepath.Join(filepath.Dir(abs), target)
		}
		abs = filepath.Clean(target)
	}
	if real, err := filepath.EvalSymlinks(abs); err == nil {
		return real
	}
	tail := ""
	cur := abs
	for {
		parent := filepath.Dir(cur)
		if parent == cur {
			return abs
		}
		tail = filepath.Join(filepath.Base(cur), tail)
		cur = parent
		if real, err := filepath.EvalSymlinks(cur); err == nil {
			return filepath.Join(real, tail)
		}
	}
}

// --- pruning --------------------------------------------------------------

// pruneEmptyNonGroupDirs removes directories that have been emptied out —
// groups a box has left, and the intermediate directories of a nested
// symlink_name that no longer holds anything.
//
// A directory whose path relative to the tree root IS a group name is kept even
// when empty.
//
// NOTE (suspected Python bug, reproduced here deliberately): the comparison is
// against group NAMES, but the directories are named after each group's
// symlink_name. For a group whose symlink_name differs from its name — which is
// almost every group in the real config, e.g. group "physics" living at
// "all/physics" — the two never match, so its directory is pruned as soon as it
// empties. Reproducing this keeps the user's ~/g free of ~30 empty directories;
// "fixing" it would leave them behind. Reported rather than changed.
func pruneEmptyNonGroupDirs(root string, groups []groupSpec) error {
	names := make(map[string]bool, len(groups))
	for _, g := range groups {
		names[g.name] = true
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("cannot read group tree directory '%s': %w", root, err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".") {
			continue
		}
		if err := pruneDir(root, filepath.Join(root, e.Name()), names); err != nil {
			return err
		}
	}
	return nil
}

func pruneDir(root, dir string, groupNames map[string]bool) error {
	fi, err := os.Lstat(dir)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("cannot inspect '%s': %w", dir, err)
	}
	if fi.Mode()&fs.ModeSymlink != 0 || !fi.IsDir() {
		return nil
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("cannot read group directory '%s': %w", dir, err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".") {
			continue
		}
		p := filepath.Join(dir, e.Name())
		if isDir(p) {
			// pruneDir returns immediately for a symlink, so a symlink to a
			// directory is a no-op rather than a descent into a box.
			if err := pruneDir(root, p, groupNames); err != nil {
				return err
			}
		}
	}

	rel, err := filepath.Rel(root, dir)
	if err != nil {
		return fmt.Errorf("cannot relativise '%s' against '%s': %w", dir, root, err)
	}
	if groupNames[filepath.ToSlash(rel)] {
		return nil
	}

	// Re-read: the recursion above may have emptied this directory.
	entries, err = os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("cannot read group directory '%s': %w", dir, err)
	}
	for _, e := range entries {
		if !strings.HasPrefix(e.Name(), ".") {
			return nil // Still holds something.
		}
	}
	if err := assertUnder(root, dir); err != nil {
		return err
	}
	// os.Remove refuses a non-empty directory, so a directory holding only
	// hidden entries fails loudly here rather than being wiped — the same
	// OSError the Python's Path.rmdir() raises.
	if err := os.Remove(dir); err != nil {
		return fmt.Errorf("cannot remove empty group directory '%s': %w", dir, err)
	}
	return nil
}

// --- helpers --------------------------------------------------------------

// assertUnder is the last line of defence before any removal: it refuses a path
// that is not strictly inside the group tree. Every path this package removes
// is built by joining onto root, so this can only fire on a programming error —
// which is exactly when it matters, because the alternative is deleting
// somewhere in the user's home directory.
func assertUnder(root, p string) error {
	rel, err := filepath.Rel(root, p)
	if err != nil {
		return fmt.Errorf("refusing to remove '%s': not resolvable against the group tree '%s': %w", p, root, err)
	}
	if rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return fmt.Errorf("refusing to remove '%s': it is outside the group tree '%s'", p, root)
	}
	return nil
}

// isDir follows symlinks, matching Python's Path.is_dir(). A broken symlink is
// not a directory.
func isDir(p string) bool {
	fi, err := os.Stat(p)
	return err == nil && fi.IsDir()
}

func isSymlink(p string) bool {
	fi, err := os.Lstat(p)
	return err == nil && fi.Mode()&fs.ModeSymlink != 0
}

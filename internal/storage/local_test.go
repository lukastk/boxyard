package storage

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/syncengine"
)

// TestLocalLastModifiedSkipsExcludedDebris pins the behaviour that Python
// v0.4.6 added after a real box wedged on it: a file the sync filters exclude
// must NOT make the box look modified. Without this a `.DS_Store` alone flips a
// box to NEEDS_PUSH, and to CONFLICT when the remote has also moved on.
func TestLocalLastModifiedSkipsExcludedDebris(t *testing.T) {
	a := New(nil)
	root := t.TempDir()

	old := time.Now().Add(-72 * time.Hour)
	real1 := filepath.Join(root, "notes.md")
	if err := os.WriteFile(real1, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(real1, old, old); err != nil {
		t.Fatal(err)
	}

	// Debris, written NOW — newer than everything real.
	if err := os.WriteFile(filepath.Join(root, ".DS_Store"), []byte("junk"), 0o644); err != nil {
		t.Fatal(err)
	}

	excl := map[string]bool{".DS_Store": true}
	got, found, err := a.LocalLastModified(root, excl)
	if err != nil || !found {
		t.Fatalf("LocalLastModified: %v found=%v", err, found)
	}
	if got.After(old.Add(time.Minute)) {
		t.Fatalf("excluded debris set the mtime: got %v, want ~%v", got, old)
	}

	// Without the exclusion the debris DOES count — proving the test is
	// measuring the exclusion and not something else.
	got2, _, err := a.LocalLastModified(root, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !got2.After(old.Add(time.Minute)) {
		t.Fatal("without exclusions the debris should have set the mtime")
	}
}

// TestLocalLastModifiedExcludesWholeDirectories — an excluded NAME prunes the
// directory, so nothing beneath it counts either.
func TestLocalLastModifiedExcludesWholeDirectories(t *testing.T) {
	a := New(nil)
	root := t.TempDir()

	old := time.Now().Add(-72 * time.Hour)
	keep := filepath.Join(root, "keep.txt")
	if err := os.WriteFile(keep, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(keep, old, old); err != nil {
		t.Fatal(err)
	}

	nm := filepath.Join(root, "node_modules", "deep")
	if err := os.MkdirAll(nm, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(nm, "fresh.js"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	got, found, err := a.LocalLastModified(root, map[string]bool{"node_modules": true})
	if err != nil || !found {
		t.Fatalf("%v found=%v", err, found)
	}
	if got.After(old.Add(time.Minute)) {
		t.Fatal("a file beneath an excluded directory set the mtime")
	}
}

// TestLocalLastModifiedUnreadableDirIsLoud — swallowing a permission error
// lowers the reported mtime, so a box with real changes underneath looks
// SYNCED and is never pushed. Data loss by omission, with no error anywhere.
func TestLocalLastModifiedUnreadableDirIsLoud(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root can read anything")
	}
	a := New(nil)
	root := t.TempDir()
	locked := filepath.Join(root, "locked")
	if err := os.MkdirAll(locked, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(locked, "f.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(locked, 0o000); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(locked, 0o755)

	if _, _, err := a.LocalLastModified(root, nil); err == nil {
		t.Fatal("an unreadable directory was silently skipped")
	}
}

// TestLocalLastModifiedMissingPathIsNotAnError — a box that has never been
// synced has no local path yet; that is expected, not a failure.
func TestLocalLastModifiedMissingPathIsNotAnError(t *testing.T) {
	a := New(nil)
	_, found, err := a.LocalLastModified(filepath.Join(t.TempDir(), "nope"), nil)
	if err != nil {
		t.Fatalf("missing path errored: %v", err)
	}
	if found {
		t.Fatal("missing path reported a modification time")
	}
}

func TestLocalIsEmptyDir(t *testing.T) {
	a := New(nil)
	root := t.TempDir()
	empty, err := a.LocalIsEmptyDir(root)
	if err != nil || !empty {
		t.Fatalf("fresh temp dir: empty=%v err=%v", empty, err)
	}
	if err := os.WriteFile(filepath.Join(root, "f"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if empty, _ := a.LocalIsEmptyDir(root); empty {
		t.Fatal("a directory with a file reported empty")
	}
}

// TestLiteralExcludeNames pins the parsing rules against Python's, including
// the deliberate refusal to interpret globs.
func TestLiteralExcludeNames(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, ".rclone_exclude")
	body := "# a comment\n\n.DS_Store\nnode_modules/\n*.tmp\n**/build\nsrc/generated\n.venv\n"
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	got := syncengine.LiteralExcludeNames(p)

	for _, want := range []string{".DS_Store", "node_modules", ".venv"} {
		if !got[want] {
			t.Errorf("literal name %q was not extracted: %v", want, got)
		}
	}
	// Globs and path patterns are NOT interpreted — reimplementing rclone's
	// filter language would be a second, subtly different implementation of
	// the thing that decides what actually transfers.
	for _, unwanted := range []string{"*.tmp", "**/build", "src/generated", "# a comment", ""} {
		if got[unwanted] {
			t.Errorf("non-literal pattern %q was treated as a name", unwanted)
		}
	}
	if len(got) != 3 {
		t.Errorf("expected exactly 3 literal names, got %v", got)
	}

	// A missing file is an ordinary state, not an error.
	if n := syncengine.LiteralExcludeNames(filepath.Join(dir, "nope")); len(n) != 0 {
		t.Errorf("missing exclude file yielded %v", n)
	}
	if n := syncengine.LiteralExcludeNames(""); len(n) != 0 {
		t.Errorf("empty path yielded %v", n)
	}
}

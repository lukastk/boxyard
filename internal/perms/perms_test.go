package perms

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/boxconst"
)

const manifestName = boxconst.BoxPermsManifestRelPath

// newBox builds the same DATA-like tree the Python test suite's `box` fixture
// does: an exec script, a plain file, a nested exec, and a symlink pointing at
// the exec script.
func newBox(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	mkdir(t, filepath.Join(root, "sub"))
	writeFile(t, filepath.Join(root, "script.sh"), "#!/bin/sh\necho hi\n", 0o755)
	writeFile(t, filepath.Join(root, "data.txt"), "plain\n", 0o644)
	writeFile(t, filepath.Join(root, "sub", "tool"), "#!/usr/bin/env python\n", 0o755)
	if err := os.Symlink("script.sh", filepath.Join(root, "link.sh")); err != nil {
		t.Fatalf("symlink: %v", err)
	}
	return root
}

func mkdir(t *testing.T, p string) {
	t.Helper()
	if err := os.MkdirAll(p, 0o755); err != nil {
		t.Fatalf("mkdir %q: %v", p, err)
	}
}

func writeFile(t *testing.T, p, content string, mode os.FileMode) {
	t.Helper()
	if err := os.WriteFile(p, []byte(content), 0o666); err != nil {
		t.Fatalf("write %q: %v", p, err)
	}
	chmod(t, p, mode)
}

func chmod(t *testing.T, p string, mode os.FileMode) {
	t.Helper()
	if err := os.Chmod(p, mode); err != nil {
		t.Fatalf("chmod %q: %v", p, err)
	}
}

func permOf(t *testing.T, p string) os.FileMode {
	t.Helper()
	fi, err := os.Stat(p)
	if err != nil {
		t.Fatalf("stat %q: %v", p, err)
	}
	return fi.Mode().Perm()
}

func isExecutable(t *testing.T, p string) bool {
	t.Helper()
	return permOf(t, p)&0o100 != 0
}

func readManifest(t *testing.T, root string) Manifest {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(root, manifestName))
	if err != nil {
		t.Fatalf("read manifest: %v", err)
	}
	m, err := decodeManifest(data)
	if err != nil {
		t.Fatalf("decode manifest: %v", err)
	}
	return m
}

// writeRawManifest drops arbitrary bytes in as the manifest, standing in for
// what an older/other implementation (or a corrupted sync) might leave behind.
func writeRawManifest(t *testing.T, root, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(root, manifestName), []byte(content), 0o666); err != nil {
		t.Fatalf("write raw manifest: %v", err)
	}
}

// ---------------------------------------------------------------------------
// BuildManifest — ports Python's TestBuildManifest
// ---------------------------------------------------------------------------

func TestBuildManifest(t *testing.T) {
	cases := []struct {
		name string
		// setup runs against the standard box fixture and may add to it.
		setup func(t *testing.T, root string)
		want  []string
	}{
		{
			// Python: test_lists_only_executables_sorted / test_excludes_symlinks.
			// link.sh points at an executable but is a symlink, so it must not
			// appear — rclone ships symlinks separately via --links.
			name:  "lists only executables, sorted, symlinks excluded",
			setup: func(t *testing.T, root string) {},
			want:  []string{"script.sh", "sub/tool"},
		},
		{
			// Python: test_prunes_unsynced_dirs. Executables inside
			// .venv / node_modules / .git must NOT be captured — boxyard does
			// not sync those, so they would be dead weight in the manifest.
			name: "prunes unsynced dirs",
			setup: func(t *testing.T, root string) {
				for _, d := range []string{".venv/bin", "node_modules/.bin", ".git/hooks", "sub/keep"} {
					mkdir(t, filepath.Join(root, d))
					writeFile(t, filepath.Join(root, d, "x"), "#!/bin/sh\n", 0o755)
				}
			},
			want: []string{"script.sh", "sub/keep/x", "sub/tool"},
		},
		{
			name: "prunes .pixi, .trunk and __pycache__ at any depth",
			setup: func(t *testing.T, root string) {
				for _, d := range []string{".pixi/envs", "sub/.trunk", "sub/__pycache__"} {
					mkdir(t, filepath.Join(root, d))
					writeFile(t, filepath.Join(root, d, "x"), "#!/bin/sh\n", 0o755)
				}
			},
			want: []string{"script.sh", "sub/tool"},
		},
		{
			// Pruning matches whole directory names only. Real manifests in
			// ~/dev contain .venv-scallop/bin/* entries.
			name: "does not prune names that merely start with a pruned name",
			setup: func(t *testing.T, root string) {
				mkdir(t, filepath.Join(root, ".venv-scallop", "bin"))
				writeFile(t, filepath.Join(root, ".venv-scallop", "bin", "f2py"), "#!/bin/sh\n", 0o755)
			},
			want: []string{".venv-scallop/bin/f2py", "script.sh", "sub/tool"},
		},
		{
			// Python: test_excludes_manifest_itself. Even a +x manifest is
			// excluded, or it would govern itself.
			name: "excludes the manifest itself even when executable",
			setup: func(t *testing.T, root string) {
				if _, err := GenerateManifest(root); err != nil {
					t.Fatalf("GenerateManifest: %v", err)
				}
				chmod(t, filepath.Join(root, manifestName), 0o755)
			},
			want: []string{"script.sh", "sub/tool"},
		},
		{
			// Only the ROOT-level manifest is excluded: Python compares
			// against `root / MANIFEST`, so a nested one is ordinary content.
			name: "does not exclude a nested manifest file",
			setup: func(t *testing.T, root string) {
				writeFile(t, filepath.Join(root, "sub", manifestName), "{}\n", 0o755)
			},
			want: []string{"script.sh", "sub/" + manifestName, "sub/tool"},
		},
		{
			name: "skips .DS_Store",
			setup: func(t *testing.T, root string) {
				writeFile(t, filepath.Join(root, ".DS_Store"), "junk", 0o755)
				writeFile(t, filepath.Join(root, "sub", ".DS_Store"), "junk", 0o755)
			},
			want: []string{"script.sh", "sub/tool"},
		},
		{
			// Group/other execute alone is not enough: Python tests S_IXUSR.
			name: "only the owner-execute bit counts",
			setup: func(t *testing.T, root string) {
				writeFile(t, filepath.Join(root, "group-x"), "x", 0o645)
				writeFile(t, filepath.Join(root, "owner-x"), "x", 0o744)
			},
			want: []string{"owner-x", "script.sh", "sub/tool"},
		},
		{
			name: "directories are never listed",
			setup: func(t *testing.T, root string) {
				mkdir(t, filepath.Join(root, "sub", "deep", "deeper"))
			},
			want: []string{"script.sh", "sub/tool"},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := newBox(t)
			tc.setup(t, root)
			m, err := BuildManifest(root)
			if err != nil {
				t.Fatalf("BuildManifest: %v", err)
			}
			if m.Version != 1 {
				t.Errorf("Version = %d, want 1", m.Version)
			}
			if !slices.Equal(m.Executable, tc.want) {
				t.Errorf("Executable =\n  %q\nwant\n  %q", m.Executable, tc.want)
			}
		})
	}
}

func TestBuildManifestEmptyTree(t *testing.T) {
	m, err := BuildManifest(t.TempDir())
	if err != nil {
		t.Fatalf("BuildManifest: %v", err)
	}
	if m.Version != 1 {
		t.Errorf("Version = %d, want 1", m.Version)
	}
	if len(m.Executable) != 0 {
		t.Errorf("Executable = %q, want empty", m.Executable)
	}
	// Must encode as `[]`, never `null` — Python's json.dumps of an empty list
	// is `[]` and the bytes are a contract.
	if got := string(m.encode()); !strings.Contains(got, `"executable": []`) {
		t.Errorf("empty manifest encoded as:\n%s", got)
	}
}

// Deliberate deviation from Python, whose os.walk silently yields nothing for
// a missing root. See the package doc.
func TestBuildManifestMissingRootErrors(t *testing.T) {
	if _, err := BuildManifest(filepath.Join(t.TempDir(), "does_not_exist")); err == nil {
		t.Fatal("expected an error for a missing root")
	}
	f := filepath.Join(t.TempDir(), "a-file")
	writeFile(t, f, "x", 0o644)
	if _, err := BuildManifest(f); err == nil {
		t.Fatal("expected an error when the root is not a directory")
	}
}

// ---------------------------------------------------------------------------
// GenerateManifest — ports Python's TestGenerate
// ---------------------------------------------------------------------------

func TestGenerateWritesManifest(t *testing.T) {
	root := newBox(t)
	wrote, err := GenerateManifest(root)
	if err != nil {
		t.Fatalf("GenerateManifest: %v", err)
	}
	if !wrote {
		t.Fatal("GenerateManifest = false, want true")
	}
	if got := readManifest(t, root).Executable; !slices.Equal(got, []string{"script.sh", "sub/tool"}) {
		t.Errorf("Executable = %q", got)
	}
}

// An unchanged box must not get a fresh mtime — boxyard's sync-status check
// would read it as a spurious edit and rclone would re-transfer the manifest.
func TestGenerateIdempotentNoRewrite(t *testing.T) {
	root := newBox(t)
	if wrote, err := GenerateManifest(root); err != nil || !wrote {
		t.Fatalf("first GenerateManifest = %v, %v", wrote, err)
	}
	fi, err := os.Stat(filepath.Join(root, manifestName))
	if err != nil {
		t.Fatal(err)
	}
	before := fi.ModTime()

	wrote, err := GenerateManifest(root)
	if err != nil {
		t.Fatalf("GenerateManifest: %v", err)
	}
	if wrote {
		t.Error("GenerateManifest = true on an unchanged box, want false")
	}
	fi, err = os.Stat(filepath.Join(root, manifestName))
	if err != nil {
		t.Fatal(err)
	}
	if !fi.ModTime().Equal(before) {
		t.Errorf("mtime changed: %v -> %v", before, fi.ModTime())
	}
}

func TestGenerateRewritesOnChange(t *testing.T) {
	root := newBox(t)
	if _, err := GenerateManifest(root); err != nil {
		t.Fatal(err)
	}
	chmod(t, filepath.Join(root, "data.txt"), 0o755)

	wrote, err := GenerateManifest(root)
	if err != nil {
		t.Fatalf("GenerateManifest: %v", err)
	}
	if !wrote {
		t.Fatal("GenerateManifest = false after a chmod, want true")
	}
	if got := readManifest(t, root).Executable; !slices.Contains(got, "data.txt") {
		t.Errorf("Executable = %q, want it to contain data.txt", got)
	}
}

// Python: test_missing_root_is_noop. A missing root is a legitimate expected
// state for the generator, reported as (false, nil) — NOT an error.
func TestGenerateMissingRootIsNoop(t *testing.T) {
	cases := []struct {
		name string
		path func(t *testing.T) string
	}{
		{"missing dir", func(t *testing.T) string { return filepath.Join(t.TempDir(), "does_not_exist") }},
		{"root is a file", func(t *testing.T) string {
			p := filepath.Join(t.TempDir(), "a-file")
			writeFile(t, p, "x", 0o644)
			return p
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			wrote, err := GenerateManifest(tc.path(t))
			if err != nil {
				t.Fatalf("GenerateManifest returned an error: %v", err)
			}
			if wrote {
				t.Error("GenerateManifest = true, want false")
			}
		})
	}
}

// Python: test_no_executables_no_manifest_created. A clean box stays clean.
func TestGenerateNoExecutablesCreatesNothing(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "a.txt"), "x", 0o644)

	wrote, err := GenerateManifest(root)
	if err != nil {
		t.Fatalf("GenerateManifest: %v", err)
	}
	if wrote {
		t.Error("GenerateManifest = true, want false")
	}
	if _, err := os.Stat(filepath.Join(root, manifestName)); !os.IsNotExist(err) {
		t.Errorf("manifest was created; stat err = %v", err)
	}
}

// Python: test_existing_manifest_kept_accurate_when_execs_removed. Once a
// manifest exists it is kept accurate, including shrinking to an empty list.
func TestGenerateKeepsExistingManifestAccurate(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "s.sh"), "#!/bin/sh\n", 0o755)

	if wrote, err := GenerateManifest(root); err != nil || !wrote {
		t.Fatalf("GenerateManifest = %v, %v; want true, nil", wrote, err)
	}
	chmod(t, filepath.Join(root, "s.sh"), 0o644)

	wrote, err := GenerateManifest(root)
	if err != nil {
		t.Fatalf("GenerateManifest: %v", err)
	}
	if !wrote {
		t.Fatal("GenerateManifest = false, want true (existing manifest must be updated)")
	}
	if got := readManifest(t, root).Executable; len(got) != 0 {
		t.Errorf("Executable = %q, want empty", got)
	}
}

// ---------------------------------------------------------------------------
// ApplyManifest — ports Python's TestApply
// ---------------------------------------------------------------------------

func TestApplyRestoresExecBit(t *testing.T) {
	root := newBox(t)
	if _, err := GenerateManifest(root); err != nil {
		t.Fatal(err)
	}
	// Simulate the transport stripping exec bits.
	chmod(t, filepath.Join(root, "script.sh"), 0o644)
	chmod(t, filepath.Join(root, "sub", "tool"), 0o644)

	changed, err := ApplyManifest(root)
	if err != nil {
		t.Fatalf("ApplyManifest: %v", err)
	}
	slices.Sort(changed)
	if !slices.Equal(changed, []string{"script.sh", "sub/tool"}) {
		t.Errorf("changed = %q", changed)
	}
	if !isExecutable(t, filepath.Join(root, "script.sh")) {
		t.Error("script.sh is not executable")
	}
	if !isExecutable(t, filepath.Join(root, "sub", "tool")) {
		t.Error("sub/tool is not executable")
	}
}

// The exec bits mirror the read bits — x is added exactly where r is set.
func TestApplyMirrorsReadBits(t *testing.T) {
	cases := []struct {
		name  string
		start os.FileMode
		want  os.FileMode
	}{
		{"644 -> 755", 0o644, 0o755},
		{"664 -> 775", 0o664, 0o775},
		{"640 -> 750", 0o640, 0o750}, // Python: test_mirrors_read_bits
		{"600 -> 700", 0o600, 0o700},
		{"604 -> 705", 0o604, 0o705},
		{"already 755 stays 755", 0o755, 0o755},
		{"no read bits at all is left alone", 0o200, 0o200},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			writeFile(t, filepath.Join(root, "script.sh"), "#!/bin/sh\n", 0o755)
			if _, err := GenerateManifest(root); err != nil {
				t.Fatal(err)
			}
			chmod(t, filepath.Join(root, "script.sh"), tc.start)
			if _, err := ApplyManifest(root); err != nil {
				t.Fatalf("ApplyManifest: %v", err)
			}
			if got := permOf(t, filepath.Join(root, "script.sh")); got != tc.want {
				t.Errorf("mode = %#o, want %#o", got, tc.want)
			}
		})
	}
}

func TestApplyLeavesNonExecAlone(t *testing.T) {
	root := newBox(t)
	if _, err := GenerateManifest(root); err != nil {
		t.Fatal(err)
	}
	if _, err := ApplyManifest(root); err != nil {
		t.Fatalf("ApplyManifest: %v", err)
	}
	if isExecutable(t, filepath.Join(root, "data.txt")) {
		t.Error("data.txt became executable")
	}
}

// THE v1 GUARANTEE: applying a manifest is additive. A file that is executable
// on disk but absent from the manifest must stay executable.
func TestApplyIsAdditiveAndNeverClears(t *testing.T) {
	root := newBox(t)
	writeRawManifest(t, root, `{"version": 1, "executable": ["script.sh"]}`)
	chmod(t, filepath.Join(root, "data.txt"), 0o755)
	chmod(t, filepath.Join(root, "sub", "tool"), 0o755)

	if _, err := ApplyManifest(root); err != nil {
		t.Fatalf("ApplyManifest: %v", err)
	}
	if !isExecutable(t, filepath.Join(root, "data.txt")) {
		t.Error("data.txt lost its exec bit; apply must never clear")
	}
	if !isExecutable(t, filepath.Join(root, "sub", "tool")) {
		t.Error("sub/tool lost its exec bit; apply must never clear")
	}
}

// setuid/setgid/sticky must survive an apply that does chmod the file.
func TestApplyPreservesSpecialBits(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "s.sh"), "#!/bin/sh\n", 0o755)
	if _, err := GenerateManifest(root); err != nil {
		t.Fatal(err)
	}
	chmod(t, filepath.Join(root, "s.sh"), os.ModeSetgid|0o644)
	if fi, err := os.Stat(filepath.Join(root, "s.sh")); err != nil {
		t.Fatal(err)
	} else if fi.Mode()&os.ModeSetgid == 0 {
		t.Skip("this filesystem does not keep the setgid bit on a plain file")
	}

	if _, err := ApplyManifest(root); err != nil {
		t.Fatalf("ApplyManifest: %v", err)
	}
	fi, err := os.Stat(filepath.Join(root, "s.sh"))
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm() != 0o755 {
		t.Errorf("perm = %#o, want 0755", fi.Mode().Perm())
	}
	if fi.Mode()&os.ModeSetgid == 0 {
		t.Error("setgid bit was cleared")
	}
}

func TestApplyIdempotent(t *testing.T) {
	root := newBox(t)
	if _, err := GenerateManifest(root); err != nil {
		t.Fatal(err)
	}
	chmod(t, filepath.Join(root, "script.sh"), 0o644)
	if _, err := ApplyManifest(root); err != nil {
		t.Fatal(err)
	}
	changed, err := ApplyManifest(root)
	if err != nil {
		t.Fatalf("ApplyManifest: %v", err)
	}
	if len(changed) != 0 {
		t.Errorf("second apply changed %q, want nothing", changed)
	}
}

// Backward compatibility: boxes created before v0.3.0 have no manifest. That
// is a legitimate expected state, not an error.
func TestApplyNoManifestIsNoop(t *testing.T) {
	changed, err := ApplyManifest(t.TempDir())
	if err != nil {
		t.Fatalf("ApplyManifest returned an error for an absent manifest: %v", err)
	}
	if len(changed) != 0 {
		t.Errorf("changed = %q, want nothing", changed)
	}
	// A root that does not exist at all is the same no-op.
	changed, err = ApplyManifest(filepath.Join(t.TempDir(), "does_not_exist"))
	if err != nil {
		t.Fatalf("ApplyManifest returned an error for a missing root: %v", err)
	}
	if len(changed) != 0 {
		t.Errorf("changed = %q, want nothing", changed)
	}
}

// Entries the local checkout does not have are skipped: the manifest describes
// the whole box, but rclone excludes may legitimately leave files out.
func TestApplySkipsUnusableEntries(t *testing.T) {
	cases := []struct {
		name    string
		execs   []string
		setup   func(t *testing.T, root string)
		changed []string
	}{
		{
			// Python: test_missing_listed_file_skipped
			name:    "missing file",
			execs:   []string{"script.sh", "gone.sh"},
			setup:   func(t *testing.T, root string) {},
			changed: []string{"script.sh"},
		},
		{
			name:    "symlink",
			execs:   []string{"link.sh", "script.sh"},
			setup:   func(t *testing.T, root string) {},
			changed: []string{"script.sh"},
		},
		{
			name:    "directory",
			execs:   []string{"script.sh", "sub"},
			setup:   func(t *testing.T, root string) {},
			changed: []string{"script.sh"},
		},
		{
			name:  "entry naming the box root itself",
			execs: []string{"", "script.sh"},
			setup: func(t *testing.T, root string) {},
			// Python's `root / ""` is the root, a directory, so it is skipped.
			changed: []string{"script.sh"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := newBox(t)
			tc.setup(t, root)
			blob, err := json.Marshal(map[string]any{"version": 1, "executable": tc.execs})
			if err != nil {
				t.Fatal(err)
			}
			writeRawManifest(t, root, string(blob))
			chmod(t, filepath.Join(root, "script.sh"), 0o644)

			changed, err := ApplyManifest(root)
			if err != nil {
				t.Fatalf("ApplyManifest: %v", err)
			}
			if !slices.Equal(changed, tc.changed) {
				t.Errorf("changed = %q, want %q", changed, tc.changed)
			}
		})
	}
}

// DELIBERATE DEVIATION from Python, which prints a warning to stderr and
// returns []. Python does that because it has no error channel; Go does, so
// the caller decides whether a corrupt manifest should fail the sync. Silently
// swallowing it here would be exactly the masked-bug fallback the repo bans.
func TestApplyMalformedManifestErrors(t *testing.T) {
	cases := []struct {
		name    string
		content string
		wantMsg string
	}{
		{"not json", "{ not valid json", "could not parse"},
		{"truncated", `{"version": 1, "executable": [`, "could not parse"},
		{"empty file", "", "could not parse"},
		{"missing executable key", `{"version": 1}`, `no "executable" key`},
		{"executable is not a list", `{"version": 1, "executable": "script.sh"}`, "could not parse"},
		{"entry is not a string", `{"version": 1, "executable": [17]}`, "expected a string"},
		{"absolute entry", `{"version": 1, "executable": ["/etc/shadow"]}`, "absolute path"},
		{"escaping entry", `{"version": 1, "executable": ["../../.ssh/authorized_keys"]}`, "escapes the box root"},
		{"lone surrogate that is not a byte escape", `{"version": 1, "executable": ["\ud800x"]}`, "lone surrogate"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := newBox(t)
			writeRawManifest(t, root, tc.content)
			chmod(t, filepath.Join(root, "script.sh"), 0o644)

			changed, err := ApplyManifest(root)
			if err == nil {
				t.Fatalf("ApplyManifest returned no error; changed = %q", changed)
			}
			if !strings.Contains(err.Error(), tc.wantMsg) {
				t.Errorf("error = %v, want it to mention %q", err, tc.wantMsg)
			}
			if !strings.Contains(err.Error(), manifestName) {
				t.Errorf("error = %v, want it to name the manifest file", err)
			}
		})
	}
}

// A manifest with no "version" key, or a future version, is still applied —
// Python's applier never looks at the field, and a Go reader that refused a
// file Python accepts would break the migration in the other direction.
func TestApplyIgnoresVersionField(t *testing.T) {
	for _, content := range []string{
		`{"executable": ["script.sh"]}`,
		`{"version": 2, "executable": ["script.sh"]}`,
		`{"version": 1, "executable": ["script.sh"], "future_key": {"a": 1}}`,
	} {
		root := newBox(t)
		writeRawManifest(t, root, content)
		chmod(t, filepath.Join(root, "script.sh"), 0o644)

		changed, err := ApplyManifest(root)
		if err != nil {
			t.Fatalf("ApplyManifest(%s): %v", content, err)
		}
		if !slices.Equal(changed, []string{"script.sh"}) {
			t.Errorf("ApplyManifest(%s) changed = %q", content, changed)
		}
	}
}

// ---------------------------------------------------------------------------
// Round trip
// ---------------------------------------------------------------------------

func TestRoundTripBuildWriteApply(t *testing.T) {
	root := newBox(t)
	mkdir(t, filepath.Join(root, "sub", "deep"))
	writeFile(t, filepath.Join(root, "sub", "deep", "run"), "#!/bin/sh\n", 0o750)
	writeFile(t, filepath.Join(root, "readme.md"), "hi\n", 0o644)

	built, err := BuildManifest(root)
	if err != nil {
		t.Fatalf("BuildManifest: %v", err)
	}
	want := []string{"script.sh", "sub/deep/run", "sub/tool"}
	if !slices.Equal(built.Executable, want) {
		t.Fatalf("built = %q, want %q", built.Executable, want)
	}
	if wrote, err := GenerateManifest(root); err != nil || !wrote {
		t.Fatalf("GenerateManifest = %v, %v", wrote, err)
	}

	// The manifest survives an encode/decode round trip unchanged.
	if got := readManifest(t, root); !slices.Equal(got.Executable, built.Executable) || got.Version != built.Version {
		t.Errorf("round-tripped manifest = %+v, want %+v", got, built)
	}

	// Simulate the SFTP transport dropping every exec bit, as it really does.
	for _, rel := range []string{"script.sh", "sub/tool", "sub/deep/run"} {
		chmod(t, filepath.Join(root, filepath.FromSlash(rel)), 0o640)
	}
	chmod(t, filepath.Join(root, "readme.md"), 0o644)

	changed, err := ApplyManifest(root)
	if err != nil {
		t.Fatalf("ApplyManifest: %v", err)
	}
	if !slices.Equal(changed, want) {
		t.Errorf("changed = %q, want %q", changed, want)
	}
	for _, rel := range want {
		if got := permOf(t, filepath.Join(root, filepath.FromSlash(rel))); got != 0o750 {
			t.Errorf("%s mode = %#o, want 0750", rel, got)
		}
	}
	if got := permOf(t, filepath.Join(root, "readme.md")); got != 0o644 {
		t.Errorf("readme.md mode = %#o, want 0644 (untouched)", got)
	}

	// And a re-generate after the apply is a no-op: the tree matches again.
	if wrote, err := GenerateManifest(root); err != nil || wrote {
		t.Errorf("GenerateManifest after apply = %v, %v; want false, nil", wrote, err)
	}
}

// ---------------------------------------------------------------------------
// Byte compatibility with the Python encoder
// ---------------------------------------------------------------------------

// goldenSimple is a verbatim copy of a REAL manifest on this machine:
// ~/dev/20260626_radkar__trading-agent-alpaca/.boxyard-perms.json, written by
// the Python implementation.
const goldenSimple = `{
 "executable": [
  "install-cron.sh",
  "run-session.sh"
 ],
 "version": 1
}
`

// goldenEmpty and goldenTricky were produced by running the installed Python
// boxyard's own boxyard._utils.perms.generate_exec_manifest over throwaway temp
// directories (see the commit message / task notes). goldenTricky pins every
// way Python's json.dumps differs from Go's encoding/json:
//
//   - `<`, `>`, `&` are NOT escaped (encoding/json escapes all three)
//   - every non-ASCII code point IS escaped, lowercase \uXXXX (Go emits raw UTF-8)
//   - U+007F is escaped even though Go considers it printable
//   - astral code points become a surrogate pair
//   - a filename byte that is not valid UTF-8 becomes \udcXX (PEP 383)
const goldenEmpty = `{
 "executable": [],
 "version": 1
}
`

const goldenTricky = `{
 "executable": [
  "a<b>&c.sh",
  "back\\slash.sh",
  "caf\u00e9.sh",
  "del\u007fchar.sh",
  "emoji\ud83d\ude80.sh",
  "plain.sh",
  "quote\".sh",
  "tab\tchar.sh",
  "\u65e5\u672c.sh",
  "\udcff-invalid-utf8.sh"
 ],
 "version": 1
}
`

// trickyPaths are the Go-side strings for the filenames in goldenTricky,
// deliberately out of order so the encoder test also pins the sort.
var trickyPaths = []string{
	"plain.sh",
	"\xff-invalid-utf8.sh", // a byte that is not valid UTF-8
	"日本.sh",
	"tab\tchar.sh",
	`quote".sh`,
	"emoji🚀.sh",
	"del\x7fchar.sh",
	"café.sh",
	`back\slash.sh`,
	"a<b>&c.sh",
}

func TestEncodeMatchesPythonBytes(t *testing.T) {
	cases := []struct {
		name  string
		execs []string
		want  string
	}{
		{
			name:  "real manifest from ~/dev",
			execs: []string{"install-cron.sh", "run-session.sh"},
			want:  goldenSimple,
		},
		{"empty list", []string{}, goldenEmpty},
		{"nil list encodes as [] not null", nil, goldenEmpty},
		{"escaping and sort order", trickyPaths, goldenTricky},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			execs := slices.Clone(tc.execs)
			sortExecPaths(execs)
			got := string(Manifest{Version: 1, Executable: execs}.encode())
			if got != tc.want {
				t.Errorf("encode() =\n%s\n(% x)\nwant\n%s\n(% x)", got, got, tc.want, tc.want)
			}
		})
	}
}

func TestDecodeMatchesPythonStrings(t *testing.T) {
	m, err := decodeManifest([]byte(goldenTricky))
	if err != nil {
		t.Fatalf("decodeManifest: %v", err)
	}
	if m.Version != 1 {
		t.Errorf("Version = %d, want 1", m.Version)
	}
	want := slices.Clone(trickyPaths)
	sortExecPaths(want)
	if !slices.Equal(m.Executable, want) {
		t.Errorf("Executable =\n  %q\nwant\n  %q", m.Executable, want)
	}
}

// Decode(encode(x)) == x, byte for byte, for everything Python can write.
func TestEncodeDecodeRoundTrip(t *testing.T) {
	for _, golden := range []string{goldenSimple, goldenEmpty, goldenTricky} {
		m, err := decodeManifest([]byte(golden))
		if err != nil {
			t.Fatalf("decodeManifest: %v", err)
		}
		if got := string(m.encode()); got != golden {
			t.Errorf("re-encode =\n%s\nwant\n%s", got, golden)
		}
	}
}

// GenerateManifest must recognise Python-written bytes as unchanged, or the
// two implementations would rewrite each other's manifest on every sync.
func TestGenerateDoesNotRewritePythonBytes(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "install-cron.sh"), "#!/bin/sh\n", 0o755)
	writeFile(t, filepath.Join(root, "run-session.sh"), "#!/bin/sh\n", 0o755)
	writeRawManifest(t, root, goldenSimple)

	wrote, err := GenerateManifest(root)
	if err != nil {
		t.Fatalf("GenerateManifest: %v", err)
	}
	if wrote {
		t.Error("GenerateManifest rewrote a manifest that Python had already written correctly")
	}
}

// Python sorts by Unicode code point, and treats a non-UTF-8 byte as its
// surrogateescape code point (U+DC80..U+DCFF) — which puts it BEFORE an astral
// character, where a plain byte sort would put it after.
func TestSortOrderMatchesPython(t *testing.T) {
	cases := []struct {
		name string
		in   []string
		want []string
	}{
		{"ascii", []string{"b", "A", "a", "B"}, []string{"A", "B", "a", "b"}},
		{"path separator sorts as its byte", []string{"sub/tool", "sub.txt", "sub"}, []string{"sub", "sub.txt", "sub/tool"}},
		{"prefix sorts first", []string{"abc", "ab"}, []string{"ab", "abc"}},
		{"non-ascii after ascii", []string{"日本", "z"}, []string{"z", "日本"}},
		{
			name: "surrogateescape byte sorts before an astral char",
			in:   []string{"\xff", "🚀"},
			want: []string{"\xff", "🚀"}, // U+DCFF < U+1F680
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := slices.Clone(tc.in)
			sortExecPaths(got)
			if !slices.Equal(got, tc.want) {
				t.Errorf("sorted = %q, want %q", got, tc.want)
			}
		})
	}
}

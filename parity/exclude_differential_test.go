package parity

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/lukastk/boxyard/internal/storage"
	"github.com/lukastk/boxyard/internal/syncengine"
)

// This is a DIFFERENTIAL, not a unit test. The Go implementations of
// literal_exclude_names and check_last_time_modified were written by reading
// the Python, and reading is exactly how the three parity gaps already found in
// this port got in. This runs both and compares.
//
// It touches no box: every case is built in a temp directory, so the sandbox
// assertions the rest of this package needs do not apply.

const pyDriver = `
import json, sys
from boxyard._utils import check_last_time_modified, literal_exclude_names
root, excl = sys.argv[1], sys.argv[2] or None
names = sorted(literal_exclude_names(excl))
ts = check_last_time_modified(root, exclude_names=set(names))
print(json.dumps({
    "names": names,
    "epoch": None if ts is None else ts.timestamp(),
}))
`

type pyResult struct {
	Names []string `json:"names"`
	Epoch *float64 `json:"epoch"`
}

// pythonBin finds an interpreter that can import boxyard. The system python3
// usually cannot: boxyard is installed as a uv TOOL, into its own venv, so the
// comparison has to run against that interpreter or it silently compares
// against nothing.
func pythonBin() string {
	home, err := os.UserHomeDir()
	if err == nil {
		uvTool := filepath.Join(home, ".local", "share", "uv", "tools", "boxyard", "bin", "python3")
		if exec.Command(uvTool, "-c", "import boxyard").Run() == nil {
			return uvTool
		}
	}
	if exec.Command("python3", "-c", "import boxyard").Run() == nil {
		return "python3"
	}
	return ""
}

func runPython(t *testing.T, root, excludeFile string) pyResult {
	t.Helper()
	// argv, never a shell string: these paths are temp dirs but the rule is the
	// rule, and box names in this project really do contain `--` and backticks.
	cmd := exec.Command(pythonBin(), "-c", pyDriver, root, excludeFile)
	out, err := cmd.Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatalf("python driver failed: %v", err)
	}
	var r pyResult
	if err := json.Unmarshal(out, &r); err != nil {
		t.Fatalf("python driver output %q: %v", out, err)
	}
	return r
}

func pythonHasBoxyard() bool { return pythonBin() != "" }

// buildCase writes a tree and returns (root, excludeFilePath).
func buildCase(t *testing.T, name string, build func(root string), excludeBody string) (string, string) {
	t.Helper()
	root := filepath.Join(t.TempDir(), name)
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	build(root)
	excl := ""
	if excludeBody != "" {
		excl = filepath.Join(t.TempDir(), ".rclone_exclude")
		if err := os.WriteFile(excl, []byte(excludeBody), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root, excl
}

func write(t *testing.T, p string, age time.Duration) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	when := time.Now().Add(-age)
	if err := os.Chtimes(p, when, when); err != nil {
		t.Fatal(err)
	}
}

func TestExcludeAndMtimeMatchPython(t *testing.T) {
	if !pythonHasBoxyard() {
		t.Skip("python3 has no boxyard installed — nothing to compare against")
	}

	// The cases are chosen around the BOUNDARIES of the two implementations —
	// glob vs literal, pruned directory vs pruned file, the path itself being
	// excluded — rather than around typical trees. A 1-in-1000 timestamp
	// divergence in this port was missed once by sampling typical values and
	// caught only by designing the sample around the encoding boundary.
	cases := []struct {
		name    string
		build   func(root string)
		exclude string
	}{
		{
			name: "debris newer than everything real",
			build: func(r string) {
				write(t, filepath.Join(r, "notes.md"), 72*time.Hour)
				write(t, filepath.Join(r, ".DS_Store"), 0)
			},
			exclude: ".DS_Store\n",
		},
		{
			name: "excluded directory holding the newest file",
			build: func(r string) {
				write(t, filepath.Join(r, "keep.txt"), 72*time.Hour)
				write(t, filepath.Join(r, "node_modules", "deep", "fresh.js"), 0)
			},
			exclude: "node_modules/\n",
		},
		{
			name: "globs must NOT be interpreted",
			build: func(r string) {
				write(t, filepath.Join(r, "keep.txt"), 72*time.Hour)
				write(t, filepath.Join(r, "scratch.tmp"), 0)
			},
			exclude: "*.tmp\n",
		},
		{
			name: "path patterns must NOT be interpreted",
			build: func(r string) {
				write(t, filepath.Join(r, "keep.txt"), 72*time.Hour)
				write(t, filepath.Join(r, "src", "generated", "a.go"), 0)
			},
			exclude: "src/generated\n**/build\n",
		},
		{
			name: "comments, blanks and trailing slashes",
			build: func(r string) {
				write(t, filepath.Join(r, "keep.txt"), 72*time.Hour)
				write(t, filepath.Join(r, ".venv", "x"), 0)
			},
			exclude: "# a comment\n\n.venv/\n   \n",
		},
		{
			name:    "empty directory",
			build:   func(r string) {},
			exclude: ".DS_Store\n",
		},
		{
			name: "everything excluded",
			build: func(r string) {
				write(t, filepath.Join(r, ".DS_Store"), 0)
			},
			exclude: ".DS_Store\n",
		},
		{
			name: "no exclude file at all",
			build: func(r string) {
				write(t, filepath.Join(r, "a.txt"), 48*time.Hour)
				write(t, filepath.Join(r, "b.txt"), 24*time.Hour)
			},
			exclude: "",
		},
		{
			name: "nested excluded name deep in the tree",
			build: func(r string) {
				write(t, filepath.Join(r, "a", "b", "keep.txt"), 72*time.Hour)
				write(t, filepath.Join(r, "a", "b", ".git", "HEAD"), 0)
			},
			exclude: ".git\n",
		},
	}

	adapter := storage.New(nil)
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			root, excl := buildCase(t, "box", c.build, c.exclude)

			py := runPython(t, root, excl)

			goNames := syncengine.LiteralExcludeNames(excl)
			var goNameList []string
			for n := range goNames {
				goNameList = append(goNameList, n)
			}
			if got, want := strings.Join(sorted(goNameList), ","), strings.Join(py.Names, ","); got != want {
				t.Errorf("exclude names differ\n  go:     %q\n  python: %q", got, want)
			}

			goTime, goFound, err := adapter.LocalLastModified(root, goNames)
			if err != nil {
				t.Fatalf("go LocalLastModified: %v", err)
			}
			if goFound != (py.Epoch != nil) {
				t.Fatalf("found differs: go=%v python=%v", goFound, py.Epoch != nil)
			}
			if !goFound {
				return
			}
			// Compare to the second: Python carries a float epoch, Go a
			// time.Time, and filesystems vary in sub-second resolution.
			if g, p := goTime.Unix(), int64(*py.Epoch); g != p {
				t.Errorf("mtime differs: go=%d python=%d (%v vs %v)",
					g, p, goTime.UTC(), time.Unix(p, 0).UTC())
			}
		})
	}
}

func sorted(xs []string) []string {
	out := append([]string(nil), xs...)
	for i := range out {
		for j := i + 1; j < len(out); j++ {
			if out[j] < out[i] {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}

package parity

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestCrossImplementationSync drives a real two-yard sync between the Go binary
// and the installed Python, in both directions.
//
// It is the only test that can see the failures that live BETWEEN the two
// implementations: the registry-JSON gap that broke every Go command on the
// real yard, the exec-bit manifest not surviving a round trip, a sync record
// one side cannot parse. Neither side's unit tests can catch those.
//
// Everything happens in t.TempDir(); the script refuses to run anywhere else.
func TestCrossImplementationSync(t *testing.T) {
	if _, err := exec.LookPath("rclone"); err != nil {
		t.Skip("rclone is not installed")
	}
	py := pythonCLI()
	if py == "" {
		t.Skip("no boxyard console script that can drive the Python CLI")
	}

	root := t.TempDir()
	goBin := filepath.Join(root, "boxyard-go")
	build := exec.Command("go", "build", "-o", goBin, "../cmd/boxyard")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("building the Go binary: %v\n%s", err, out)
	}

	// argv, never a shell string: the paths are temp dirs, but the rule is the
	// rule.
	cmd := exec.Command("sh", "cross_impl_sync.sh", filepath.Join(root, "yards"), goBin, py)
	// A clean DEFAULT_BOX_GROUPS: this machine's shell exports one, and it
	// would otherwise be merged into the throwaway yards' config and show up in
	// the box's groups.
	cmd.Env = append(os.Environ(), "DEFAULT_BOX_GROUPS=")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("cross-implementation sync failed: %v\n%s", err, out)
	}

	text := string(out)
	for _, want := range []string{
		"hello from go",             // Go pushed, Python pulled
		"run.sh is executable in B", // the exec-bit manifest round-tripped
		"hello from python",         // Python pushed, Go pulled
		"box-status IDENTICAL across implementations",
		"OK",
	} {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q in:\n%s", want, text)
		}
	}
}

// pythonCLI finds the `boxyard` console script that goes with an interpreter
// able to import boxyard. The system one usually cannot: boxyard is installed
// as a uv TOOL, into its own venv.
func pythonCLI() string {
	py := pythonBin()
	if py == "" {
		return ""
	}
	cli := filepath.Join(filepath.Dir(py), "boxyard")
	if _, err := os.Stat(cli); err != nil {
		return ""
	}
	return cli
}

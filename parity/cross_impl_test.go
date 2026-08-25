package parity

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
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
		"yard B now knows the box",  // Go's sync-missing-meta discovered it
		"hello from go",             // Go pushed, Python pulled
		"run.sh is executable in B", // the exec-bit manifest round-tripped
		"hello from python",         // Python pushed, Go pulled
		// Go's own exclude removes the DATA, and its include puts it back
		// WITH the exec bit — the manifest has to survive a Go->Go round trip
		// as well as a cross-implementation one.
		"DATA gone from A after exclude",
		"run.sh still executable after a Go include",
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
//
// BOXYARD_PARITY_CLI overrides it, which is how these tests are pointed at a
// development checkout instead of the deployed tool — the deployed one always
// lags the fixes being ported.
func pythonCLI() string {
	if override := os.Getenv("BOXYARD_PARITY_CLI"); override != "" {
		return override
	}
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

// TestNewBoxFlagsMatchPython runs `new --parent` (all three documented forms)
// and `new --group` through BOTH implementations and compares what each yard
// ends up holding.
//
// The interesting part is not that they agree on the happy path, but that they
// agree on the REFUSALS: --group goes through modify_boxmeta, which is what
// enforces the virtual-group and unique-name rules, and a port that skipped
// them would look perfectly healthy until a box quietly joined a computed
// group.
func TestNewBoxFlagsMatchPython(t *testing.T) {
	py := pythonCLI()
	if py == "" {
		t.Skip("no boxyard console script that can drive the Python CLI")
	}
	// `--parent <index name>` and `--parent <box id>` only started working in
	// Python v0.5.8. Gated explicitly rather than left to fail: a parity test
	// that fails because the INSTALLED Python is older says nothing about the
	// port, and a skip for an unexamined reason is indistinguishable from a
	// pass.
	// The version gate applies to the DEPLOYED tool only. An explicitly
	// overridden CLI is taken at its word — an editable checkout reports the
	// version its distribution metadata was written with, not the one in
	// pyproject.toml, so gating on it there would skip forever.
	if os.Getenv("BOXYARD_PARITY_CLI") == "" {
		if v := pythonVersion(py); v != "" && versionLess(v, "0.5.8") {
			t.Skipf("the installed boxyard is %s; `--parent <index name>` needs v0.5.8", v)
		}
	}

	root := t.TempDir()
	goBin := filepath.Join(root, "boxyard-go")
	if out, err := exec.Command("go", "build", "-o", goBin, "../cmd/boxyard").CombinedOutput(); err != nil {
		t.Fatalf("building the Go binary: %v\n%s", err, out)
	}

	cmd := exec.Command("sh", "new_box_flags.sh", filepath.Join(root, "yards"), goBin, py)
	cmd.Env = append(os.Environ(), "DEFAULT_BOX_GROUPS=")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("new-flag comparison failed: %v\n%s", err, out)
	}

	// Reduce each implementation's section to the facts, dropping the box ids,
	// which differ by construction.
	sections := map[string][]string{}
	current := ""
	for _, line := range strings.Split(string(out), "\n") {
		trimmed := strings.TrimSpace(line)
		switch {
		case strings.HasPrefix(trimmed, "GO "):
			current = "GO"
		case strings.HasPrefix(trimmed, "PY "):
			current = "PY"
		}
		if current == "" {
			continue
		}
		if strings.Contains(trimmed, "groups=") || strings.Contains(trimmed, "refused") ||
			strings.Contains(trimmed, "NOT REFUSED") {
			sections[current] = append(sections[current], normaliseFacts(trimmed))
		}
	}

	if len(sections["GO"]) == 0 || len(sections["PY"]) == 0 {
		t.Fatalf("could not read both sections from:\n%s", out)
	}
	sort.Strings(sections["GO"])
	sort.Strings(sections["PY"])
	if strings.Join(sections["GO"], "\n") != strings.Join(sections["PY"], "\n") {
		t.Fatalf("implementations disagree\nGo:\n%s\n\nPython:\n%s\n\nfull output:\n%s",
			strings.Join(sections["GO"], "\n"), strings.Join(sections["PY"], "\n"), out)
	}
	if !strings.Contains(string(out), "refused") {
		t.Errorf("neither implementation refused the virtual group:\n%s", out)
	}
}

// normaliseFacts strips the parts that differ by construction: the random box
// subid, and the timestamp prefix of a parent id.
var boxIDPattern = regexp.MustCompile(`\d{8}(_\d{6})?_[A-Za-z0-9]+`)

func normaliseFacts(line string) string {
	return boxIDPattern.ReplaceAllString(line, "<box-id>")
}

// pythonVersion reports the installed boxyard's version, or "" if it cannot be
// read.
func pythonVersion(cli string) string {
	out, err := exec.Command(cli, "--version").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// versionLess compares dotted numeric versions COMPONENT BY COMPONENT.
//
// Not a string comparison: "0.5.10" sorts before "0.5.8" lexicographically,
// and a version gate that silently inverts once the patch number reaches two
// digits is the kind of thing nobody notices until a test has been skipping for
// months.
func versionLess(a, b string) bool {
	as, bs := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < len(as) || i < len(bs); i++ {
		av, bv := 0, 0
		if i < len(as) {
			av, _ = strconv.Atoi(as[i])
		}
		if i < len(bs) {
			bv, _ = strconv.Atoi(bs[i])
		}
		if av != bv {
			return av < bv
		}
	}
	return false
}

// TestGroupAndParentCommandsMatchPython runs add-to-group, remove-from-group,
// add-parent, remove-parent and create-user-symlinks through BOTH
// implementations and compares the resulting yards.
//
// The already-there / not-there paths are included on purpose: their EXIT CODES
// are asymmetric in the Python (add-parent on a parent it already has exits 0,
// remove-parent on one it does not have exits 1), and that asymmetry is the
// contract, not an accident to tidy up in the port.
func TestGroupAndParentCommandsMatchPython(t *testing.T) {
	py := pythonCLI()
	if py == "" {
		t.Skip("no boxyard console script that can drive the Python CLI")
	}

	root := t.TempDir()
	goBin := filepath.Join(root, "boxyard-go")
	if out, err := exec.Command("go", "build", "-o", goBin, "../cmd/boxyard").CombinedOutput(); err != nil {
		t.Fatalf("building the Go binary: %v\n%s", err, out)
	}

	cmd := exec.Command("sh", "group_parent_cmds.sh", filepath.Join(root, "yards"), goBin, py)
	cmd.Env = append(os.Environ(), "DEFAULT_BOX_GROUPS=")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("comparison failed: %v\n%s", err, out)
	}

	goFacts, pyFacts := splitFacts(string(out))
	if len(goFacts) == 0 || len(pyFacts) == 0 {
		t.Fatalf("could not read both sections from:\n%s", out)
	}
	if strings.Join(goFacts, "\n") != strings.Join(pyFacts, "\n") {
		t.Fatalf("implementations disagree\nGo:\n%s\n\nPython:\n%s\n\nfull output:\n%s",
			strings.Join(goFacts, "\n"), strings.Join(pyFacts, "\n"), out)
	}
}

// splitFacts collects the comparable lines of each implementation's section,
// with box ids normalised away.
func splitFacts(out string) (goFacts, pyFacts []string) {
	current := ""
	for _, line := range strings.Split(out, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "GO ") || strings.HasPrefix(trimmed, "GO:") {
			current = "GO"
		} else if strings.HasPrefix(trimmed, "PY ") || strings.HasPrefix(trimmed, "PY:") {
			current = "PY"
		}
		if current == "" || trimmed == "" {
			continue
		}
		if !strings.Contains(trimmed, "groups=") && !strings.Contains(trimmed, "nparents=") &&
			!strings.Contains(trimmed, "remove-parent") && !strings.HasPrefix(trimmed, "./") {
			continue
		}
		fact := normaliseFacts(strings.TrimPrefix(strings.TrimPrefix(trimmed, "GO:"), "PY:"))
		if current == "GO" {
			goFacts = append(goFacts, fact)
		} else {
			pyFacts = append(pyFacts, fact)
		}
	}
	sort.Strings(goFacts)
	sort.Strings(pyFacts)
	return goFacts, pyFacts
}

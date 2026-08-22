package parity

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// Impl identifies which boxyard implementation to run.
type Impl struct {
	Name string // "python" or "go"
	Bin  string // path to the executable
}

// Result is one command invocation's observable output — everything a caller
// of the CLI can see, and therefore everything parity must hold for.
type Result struct {
	Stdout   string
	Stderr   string
	ExitCode int
	Err      error
}

// Run executes one boxyard command inside the sandbox.
//
// Two gates, both re-checked on EVERY invocation because they are cheap and the
// failure they prevent is unrecoverable:
//
//  1. AssertSandboxed — the configuration describes an isolated sandbox.
//  2. isolationVerified — the BINARY was observed actually reading it.
//
// Gate 2 exists because gate 1 alone is not enough, and that gap has already
// bitten once. The Python CLI does not honour BOXYARD_CONFIG_PATH: its
// entrypoint hardcodes const.DEFAULT_CONFIG_PATH, so a perfectly valid sandbox
// config was silently ignored and `boxyard new` created a real box in the
// user's real ~/dev. A guard that only inspects configuration cannot catch a
// binary that never reads it. See VerifyIsolation.
func (s *Sandbox) Run(impl Impl, args ...string) Result {
	if err := AssertSandboxed(s); err != nil {
		return Result{ExitCode: -1, Err: fmt.Errorf("refusing to run %s: %w", impl.Name, err)}
	}
	if !s.isolationVerified {
		return Result{ExitCode: -1, Err: fmt.Errorf(
			"refusing to run %s: isolation not yet verified for this sandbox — call VerifyIsolation first", impl.Name)}
	}
	return s.run(impl, args...)
}

// run is the unguarded-by-gate-2 form, used by VerifyIsolation itself.
func (s *Sandbox) run(impl Impl, args ...string) Result {
	// --config is the ONLY mechanism that actually redirects the Python CLI,
	// and it is an option on the top-level callback, so it must precede the
	// subcommand. The env var is set too (Go honours it) but is not relied on.
	full := append([]string{"--config", s.ConfigPath}, args...)
	cmd := exec.Command(impl.Bin, full...)
	cmd.Env = s.Env()
	cmd.Dir = s.Root

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()
	res := Result{Stdout: stdout.String(), Stderr: stderr.String()}
	if err != nil {
		var ee *exec.ExitError
		if ok := asExitError(err, &ee); ok {
			res.ExitCode = ee.ExitCode()
		} else {
			res.ExitCode = -1
			res.Err = err
		}
	}
	return res
}

func asExitError(err error, target **exec.ExitError) bool {
	if ee, ok := err.(*exec.ExitError); ok {
		*target = ee
		return true
	}
	return false
}

// Trimmed returns stdout with surrounding whitespace removed — the form shell
// callers actually consume, e.g. cd $(boxyard path ...).
func (r Result) Trimmed() string { return strings.TrimSpace(r.Stdout) }

// Lines returns stdout split into non-empty trimmed lines.
func (r Result) Lines() []string {
	var out []string
	for _, l := range strings.Split(r.Stdout, "\n") {
		if l = strings.TrimSpace(l); l != "" {
			out = append(out, l)
		}
	}
	return out
}

func (r Result) String() string {
	return fmt.Sprintf("exit=%d\n--- stdout ---\n%s--- stderr ---\n%s", r.ExitCode, r.Stdout, r.Stderr)
}

// Timed runs a command and reports how long it took, for the latency
// comparison that motivated the rewrite.
func (s *Sandbox) Timed(impl Impl, args ...string) (Result, time.Duration) {
	start := time.Now()
	res := s.Run(impl, args...)
	return res, time.Since(start)
}

// VerifyIsolation proves that impl's binary actually reads the sandbox, rather
// than merely that the sandbox is well-formed. It must pass before Run will
// execute anything.
//
// The check is deliberately positive AND negative:
//
//   - positive: a freshly provisioned sandbox lists zero boxes;
//   - negative: the output contains none of the real yard's box names.
//
// The negative half is the one that matters. A binary that fell back to the
// real config would list hundreds of real boxes, and naming them explicitly
// makes the failure unmistakable rather than a puzzling count mismatch.
func (s *Sandbox) VerifyIsolation(impl Impl) error {
	if err := AssertSandboxed(s); err != nil {
		return err
	}
	res := s.run(impl, "list")
	if res.ExitCode != 0 {
		return fmt.Errorf("isolation probe (%s list) failed: %s", impl.Name, res)
	}
	lines := res.Lines()

	realNames, err := sampleRealBoxNames(8)
	if err != nil {
		return fmt.Errorf("cannot sample real box names to probe against: %w", err)
	}
	for _, real := range realNames {
		if strings.Contains(res.Stdout, real) {
			return fmt.Errorf(
				"ISOLATION FAILED: %s is reading the REAL boxyard — its output names %q.\n"+
					"Sandbox config was %s. Do not run any mutating command.",
				impl.Name, real, s.ConfigPath)
		}
	}
	if len(lines) != 0 {
		return fmt.Errorf("ISOLATION SUSPECT: a fresh sandbox should list 0 boxes, %s listed %d",
			impl.Name, len(lines))
	}

	s.isolationVerified = true
	return nil
}

// sampleRealBoxNames reads a few index names out of the user's real metadata,
// purely to assert their ABSENCE from sandboxed output.
func sampleRealBoxNames(n int) ([]string, error) {
	local, _, err := realConfigPaths()
	if err != nil {
		return nil, err
	}
	var metaPath string
	for _, p := range local {
		candidate := filepath.Join(p, "boxyard_meta.json")
		if _, err := os.Stat(candidate); err == nil {
			metaPath = candidate
			break
		}
	}
	if metaPath == "" {
		return nil, fmt.Errorf("could not locate the real boxyard_meta.json")
	}
	raw, err := os.ReadFile(metaPath)
	if err != nil {
		return nil, err
	}
	var meta struct {
		BoxMetas []struct {
			Name string `json:"name"`
		} `json:"box_metas"`
	}
	if err := json.Unmarshal(raw, &meta); err != nil {
		return nil, err
	}
	if len(meta.BoxMetas) == 0 {
		return nil, fmt.Errorf("real boxyard_meta.json lists no boxes; probe would be toothless")
	}
	var names []string
	for _, bm := range meta.BoxMetas {
		if bm.Name == "" {
			continue
		}
		names = append(names, bm.Name)
		if len(names) >= n {
			break
		}
	}
	return names, nil
}

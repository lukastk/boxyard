//go:build parity

// Smoke test for the parity harness itself.
//
// Build-tagged so it never runs under a plain `go test ./...`. It touches the
// real SFTP storage box (inside the sandbox prefix) and takes a few seconds.
//
//	go test -tags parity ./parity/ -run TestSandbox -v
package parity

import (
	"os"
	"strings"
	"testing"
)

func pythonImpl(t *testing.T) Impl {
	t.Helper()
	bin := os.Getenv("BOXYARD_PY_BIN")
	if bin == "" {
		home, _ := os.UserHomeDir()
		bin = home + "/.local/bin/boxyard"
	}
	if _, err := os.Stat(bin); err != nil {
		t.Skipf("python boxyard not found at %s", bin)
	}
	return Impl{Name: "python", Bin: bin}
}

// newProvisionedSandbox builds a sandbox, guards it, provisions it, and
// registers teardown plus the canary check.
func newProvisionedSandbox(t *testing.T) *Sandbox {
	t.Helper()

	canary, err := TakeCanary()
	if err != nil {
		t.Fatalf("could not stamp the real boxyard: %v", err)
	}

	s, err := NewSandbox(t.TempDir())
	if err != nil {
		t.Fatalf("NewSandbox: %v", err)
	}
	if err := s.Provision(); err != nil {
		t.Fatalf("Provision: %v", err)
	}

	// Prove the binary actually READS the sandbox before anything is allowed
	// to mutate. A valid sandbox config is not enough — the Python CLI
	// silently ignores BOXYARD_CONFIG_PATH, and this gate is what turns that
	// from "created a real box in ~/dev" into a clean refusal.
	if err := s.VerifyIsolation(pythonImpl(t)); err != nil {
		t.Fatalf("%v", err)
	}

	t.Cleanup(func() {
		if err := s.Teardown(); err != nil {
			t.Errorf("teardown left remote state behind: %v", err)
		}
		// The canary is the last word. If it trips, the run escaped the
		// sandbox and that is an incident, not a test failure.
		if err := canary.Verify(); err != nil {
			t.Fatalf("%v", err)
		}
	})
	return s
}

func TestSandboxIsolatesTheRealYard(t *testing.T) {
	s := newProvisionedSandbox(t)
	py := pythonImpl(t)

	// The sandbox must be empty — emphatically NOT the user's 583 boxes.
	res := s.Run(py, "list")
	if res.ExitCode != 0 {
		t.Fatalf("list failed in the sandbox:\n%s", res)
	}
	if n := len(res.Lines()); n != 0 {
		t.Fatalf("sandbox should start with 0 boxes, saw %d:\n%s", n, res.Stdout)
	}

	// And it must not be reading the real config.
	if strings.Contains(res.Stdout, "boxyard-go") || strings.Contains(res.Stdout, "mysetup") {
		t.Fatalf("sandbox appears to be reading the REAL yard:\n%s", res.Stdout)
	}
}

func TestSandboxRoundTripsABox(t *testing.T) {
	s := newProvisionedSandbox(t)
	py := pythonImpl(t)

	res := s.Run(py, "new", "--box-name", "parity-probe")
	if res.ExitCode != 0 {
		t.Fatalf("new failed:\n%s", res)
	}
	indexName := res.Trimmed()
	if !strings.HasSuffix(indexName, "__parity-probe") {
		t.Fatalf("unexpected index name %q\n%s", indexName, res)
	}

	// It should now be listed.
	if lines := s.Run(py, "list").Lines(); len(lines) != 1 {
		t.Fatalf("expected exactly 1 box after new, got %v", lines)
	}

	// Push it to the sandbox's remote prefix.
	if res := s.Run(py, "sync", "-r", indexName); res.ExitCode != 0 {
		t.Fatalf("sync failed:\n%s", res)
	}

	tree, err := s.RemoteTree()
	if err != nil {
		t.Fatalf("RemoteTree: %v", err)
	}
	if len(tree) == 0 {
		t.Fatal("sync reported success but the remote prefix is empty")
	}
	var sawBox bool
	for _, p := range tree {
		if strings.Contains(p, "parity-probe") {
			sawBox = true
		}
	}
	if !sawBox {
		t.Fatalf("box not found under the sandbox remote prefix; tree=%v", tree)
	}
	t.Logf("remote tree under %s:\n  %s", s.RemotePrefix, strings.Join(tree, "\n  "))
}

// The harness must refuse to run anything against an unguarded sandbox, even
// if a caller hands it one directly.
func TestRunRefusesUnguardedSandbox(t *testing.T) {
	py := pythonImpl(t)
	home, _ := os.UserHomeDir()
	bad := &Sandbox{
		Root:           home,
		ConfigPath:     home + "/.config/boxyard/config.toml",
		DataPath:       home + "/.boxyard",
		UserBoxesPath:  home + "/dev",
		UserGroupsPath: home + "/g",
		RemotePrefix:   "boxyard",
	}
	res := bad.Run(py, "list")
	if res.Err == nil {
		t.Fatal("harness RAN a command against the real yard")
	}
	if res.ExitCode != -1 {
		t.Errorf("expected refusal, got exit %d", res.ExitCode)
	}
}

// The gate that would have prevented the ~/dev incident: a sandbox that has
// not had its isolation verified must refuse to run anything at all.
func TestRunRefusesUntilIsolationVerified(t *testing.T) {
	py := pythonImpl(t)
	s, err := NewSandbox(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Provision(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Teardown() })

	res := s.Run(py, "new", "--box-name", "should-never-happen")
	if res.Err == nil {
		t.Fatal("harness ran a MUTATING command before isolation was verified")
	}
	if !strings.Contains(res.Err.Error(), "isolation not yet verified") {
		t.Errorf("unexpected refusal reason: %v", res.Err)
	}
}

// Package pyref locates the Python boxyard that the Go port is differentialled
// against.
//
// It is used only by tests, but it is a normal package rather than a test
// helper because the differentials live in more than one package
// (internal/cli's formatter comparison and the whole parity suite) and a
// second copy of "where is the interpreter" would drift — and drift here is
// silent, because a differential with no interpreter simply skips.
package pyref

import (
	"os"
	"os/exec"
	"path/filepath"
)

// Bin returns the path of an interpreter that can import boxyard, or "" if
// there is none.
//
// The system python3 usually cannot: boxyard is installed as a uv TOOL, into
// its own venv, so a comparison has to run against that interpreter or it
// silently compares against nothing.
func Bin() string {
	// An explicit override, so a differential can be pointed at a SOURCE
	// checkout instead of the installed tool. Without it a differential for
	// unreleased behaviour can only ever skip, and a test that has never once
	// been seen to pass is not evidence of anything. Unset in normal runs, so
	// the default remains "compare against what is actually installed".
	if override := os.Getenv("BOXYARD_PYREF_BIN"); override != "" {
		return override
	}
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

// HasSymbol reports whether the reference interpreter can import `name` from
// `module`.
//
// Differentials in this repo compare against the INSTALLED Python, not the
// source tree, which is what makes them catch real drift. The cost is that a
// differential for something the Python has not RELEASED yet cannot pass: the
// symbol is simply not there.
//
// Failing in that case would leave CI permanently red, and a permanently red
// test is one nobody reads — which defeats the differential. So a differential
// for unreleased behaviour probes first and SKIPS, with a message naming what
// is missing. The skip clears itself the moment the fleet is upgraded, and
// because the message names the symbol it cannot be mistaken for "no
// interpreter".
//
// It is deliberately NOT used to paper over a genuine mismatch: it answers
// "does this exist", never "do the two agree".
func HasSymbol(module, name string) bool {
	bin := Bin()
	if bin == "" {
		return false
	}
	script := "import importlib, sys; m = importlib.import_module(sys.argv[1]); sys.exit(0 if hasattr(m, sys.argv[2]) else 1)"
	return exec.Command(bin, "-c", script, module, name).Run() == nil
}

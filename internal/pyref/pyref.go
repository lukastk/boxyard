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

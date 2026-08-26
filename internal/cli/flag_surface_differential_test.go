package cli

import (
	"encoding/json"
	"os/exec"
	"sort"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/pyref"
	"github.com/spf13/pflag"
)

// The CLI surface is a hard contract — ~40 call sites across myrig's zsh
// functions, mysystem's TypeScript and a sesh plugin drive it, and during the
// rollout the two implementations have to be interchangeable. Comparing
// --help output cannot check that: rich wraps and TRUNCATES long option names
// into its panel, so a diff of the rendered help silently "matches" on names
// it never showed in full.
//
// This asks click for the parsed option list instead, which is exactly what
// the parser will accept, and compares it against cobra's flag set in both
// directions. It found `boxyard multi-sync --print-skipped`: a friendlier
// spelling invented by the port for a flag typer calls
// `--no-no-print-skipped`, accepted by the Go and rejected with exit 2 by the
// Python.
const pyFlagDriver = `
import json
import typer.main
from boxyard._cli.app import app

cli = typer.main.get_command(app)
out = {}
for name, sub in cli.commands.items():
    opts = set()
    for p in sub.params:
        # Positional arguments have no option strings; they are compared by
        # cobra's own arg validation, not here.
        opts.update(o for o in p.opts if o.startswith("-"))
        opts.update(o for o in p.secondary_opts if o.startswith("-"))
    out[name] = sorted(opts)
print(json.dumps(out))
`

func TestFlagSurfaceMatchesPython(t *testing.T) {
	py := pyref.Bin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}

	out, err := exec.Command(py, "-c", pyFlagDriver).Output()
	if err != nil {
		t.Fatalf("running the Python driver: %v", err)
	}
	var pySurface map[string][]string
	if err := json.Unmarshal(out, &pySurface); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}
	if len(pySurface) == 0 {
		t.Fatal("the Python reported no commands — the comparison would be vacuous")
	}

	root := NewRootCommand()
	goCommands := map[string]bool{}
	for _, cmd := range root.Commands() {
		goCommands[cmd.Name()] = true
	}

	for name, pyOpts := range pySurface {
		sub, _, err := root.Find([]string{name})
		if err != nil || sub == root {
			t.Errorf("`boxyard %s` exists in the Python and not in the Go", name)
			continue
		}
		delete(goCommands, name)

		var goOpts []string
		sub.Flags().VisitAll(func(f *pflag.Flag) {
			// --config and --version are persistent flags on the Go root, so
			// cobra offers them on every subcommand; typer keeps them on the
			// root only. Both parse the same at the root, which is where
			// anyone passes them.
			if f.Name == "config" || f.Name == "version" || f.Name == "help" {
				return
			}
			goOpts = append(goOpts, "--"+f.Name)
			if f.Shorthand != "" {
				goOpts = append(goOpts, "-"+f.Shorthand)
			}
		})
		var filteredPy []string
		for _, o := range pyOpts {
			if o == "--help" {
				continue
			}
			filteredPy = append(filteredPy, o)
		}
		sort.Strings(goOpts)
		sort.Strings(filteredPy)
		if strings.Join(goOpts, " ") != strings.Join(filteredPy, " ") {
			t.Errorf("`boxyard %s` flag surface differs:\n  python: %s\n  go:     %s",
				name, strings.Join(filteredPy, " "), strings.Join(goOpts, " "))
		}
	}

	for name := range goCommands {
		if name == "help" || name == "completion" {
			continue
		}
		t.Errorf("`boxyard %s` exists in the Go and not in the Python", name)
	}
}

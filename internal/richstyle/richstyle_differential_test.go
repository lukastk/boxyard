package richstyle

import (
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/pyref"
)

// rich is the reference. These bytes are the output contract on a terminal,
// and they are easy to get almost-right — the attribute/colour ORDER, the
// reset back to the ENCLOSING style rather than to nothing, and rich's
// backslash-escaping rules are each a place where a hand-written renderer
// diverges in a way no pipe-based comparison would ever show.
const pyRenderDriver = `
import json, sys, io, os
# This process inherits whatever the caller's shell has set, and rich reads
# FORCE_COLOR/NO_COLOR from the environment. Clear them, or the driver reports
# the harness's terminal settings instead of the flags it was asked about --
# which is how the first run of this differential "failed" on cases where the
# two sides actually agreed.
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("NO_COLOR", None)
from rich.console import Console
from rich.text import Text
from rich.markup import escape

cases = json.loads(sys.argv[1])
out = []
for kind, payload in cases:
    if kind == "escape":
        out.append(escape(payload))
        continue
    markup, enable, no_colour = payload
    # color_system mirrors what a default Console() resolves to: rich renders a
    # style only when it HAS a colour system, and _detect_color_system returns
    # None off a terminal. Forcing "truecolor" here would make rich emit
    # escapes even with force_terminal=False, which is a property of this
    # driver rather than of rich.
    c = Console(file=io.StringIO(), force_terminal=bool(enable), no_color=bool(no_colour),
                color_system="truecolor" if enable else None, width=10000, soft_wrap=True)
    try:
        c.print(Text.from_markup(markup), end="")
    except Exception as e:
        out.append("ERROR: " + type(e).__name__)
        continue
    out.append(c.file.getvalue())
print(json.dumps(out))
`

// markups covers every string the two implementations build, plus the shapes
// that separate a correct renderer from a plausible one.
var markups = []string{
	"plain text, no markup at all",
	"[bold]just bold[/bold]",
	"[dim]just dim[/dim]",
	"[bold green]Success[/bold green]",
	"[bold yellow]Read-only[/bold yellow]",
	"[bold blue]Local[/bold blue]",
	"[bold magenta]Interrupted[/bold magenta]",
	"[bold red]Error[/bold red]",
	"[green]Synced[/green]",
	"[blue]Skipped[/blue]",
	"[yellow]Write denied[/yellow]",
	"[red]something went wrong[/red]",
	// The empty-colour tag the Python's own status map produces for a status
	// it has no colour for.
	"[bold ]Syncing...[/bold ]",
	// Nesting: the close must restore the ENCLOSING style, not clear it.
	"[dim]outer [bold]inner[/bold] outer again[/dim]",
	"[bold][green]two tags[/green][/bold]",
	// Text either side of the styled run.
	"(1/586) [bold green]20260822_tsl6xn__boxyard-go[/bold green] ... [bold green]Success[/bold green]",
	// An escaped bracket must survive as a literal.
	`plain \[not-a-tag] more`,
	`[dim]\[ctx/mac, archived][/dim]`,
	// A bracket that is not tag-shaped is left alone by rich.
	"weird[Name] (20260822_tsl6xn)",
	"[bold]weird\\[name][/bold]",
	// Adjacent tags with no text between them.
	"[bold][/bold][dim][/dim]",
	// A style word rich knows and this package does not must be a loud error,
	// so it is deliberately NOT in this list -- see TestUnsupportedStyleIsLoud.
}

var escapeCases = []string{
	"plain-name", "weird[name]", "[archived]", "a[b]c[d]e",
	"trailing-backslash\\", "double\\\\", "\\[already-escaped]",
	"[UPPER]", "[123]", "[#hex]", "[/close]", "[@at]",
	"åéîøü[ctx/mac]", "", "[", "]", "[]",
}

func TestRenderMatchesRich(t *testing.T) {
	py := pyref.Bin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}

	type flags struct{ enable, noColour bool }
	combos := []flags{{true, false}, {false, false}, {true, true}, {false, true}}

	var cases [][2]any
	var want []string
	for _, m := range markups {
		for _, f := range combos {
			cases = append(cases, [2]any{"render", []any{m, f.enable, f.noColour}})
			got, err := Render(m, f.enable, f.noColour)
			if err != nil {
				t.Fatalf("Render(%q, %v, %v): %v", m, f.enable, f.noColour, err)
			}
			want = append(want, got)
		}
	}
	for _, s := range escapeCases {
		cases = append(cases, [2]any{"escape", s})
		want = append(want, Escape(s))
	}

	payload, err := json.Marshal(cases)
	if err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(py, "-c", pyRenderDriver, string(payload)).Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatalf("python driver failed: %v", err)
	}
	var got []string
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}
	if len(got) != len(want) {
		t.Fatalf("rich produced %d results, this package %d", len(got), len(want))
	}
	for i := range got {
		if got[i] != want[i] {
			t.Errorf("case %d (%v):\n  rich: %q\n  go:   %q", i, cases[i], got[i], want[i])
		}
	}
	if !strings.Contains(strings.Join(got, ""), "\x1b[") {
		t.Fatal("rich emitted no escapes at all — the comparison is vacuous")
	}
}

func TestUnsupportedStyleIsLoud(t *testing.T) {
	// A style the Python adds later must not render as plain text here. Silent
	// degradation is what makes a styling gap invisible.
	if _, err := Render("[italic]x[/italic]", true, false); err == nil {
		t.Fatal("an unsupported style rendered without complaint")
	}
	if _, err := Render("[bold]unclosed", true, false); err == nil {
		t.Fatal("an unclosed tag rendered without complaint")
	}
	if _, err := Render("x[/bold]", true, false); err == nil {
		t.Fatal("a stray closing tag rendered without complaint")
	}
}

// pyEnabledDriver asks a DEFAULT Console -- the one boxyard actually builds --
// whether it would render styles, under a given environment.
const pyEnabledDriver = `
import json, sys, os, io
from rich.console import Console

cases = json.loads(sys.argv[1])
out = []
for env in cases:
    for k in ("FORCE_COLOR", "NO_COLOR", "TERM", "TTY_COMPATIBLE", "COLORTERM"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    # A pipe, which is what both sides see here: stdout is captured.
    c = Console(file=io.StringIO())
    out.append({"enabled": c.color_system is not None, "no_color": bool(c.no_color)})
print(json.dumps(out))
`

func TestEnabledMatchesRich(t *testing.T) {
	py := pyref.Bin()
	if py == "" {
		t.Skip("no interpreter that can import boxyard")
	}

	envs := []map[string]string{
		{},
		{"FORCE_COLOR": "1"},
		{"FORCE_COLOR": "3"},
		// Set-but-empty means NO. Most tools read it the other way round.
		{"FORCE_COLOR": ""},
		{"FORCE_COLOR": "0"},
		{"NO_COLOR": "1"},
		{"NO_COLOR": ""},
		{"FORCE_COLOR": "1", "NO_COLOR": "1"},
		// TTY_COMPATIBLE is checked before FORCE_COLOR and decides outright.
		{"TTY_COMPATIBLE": "1"},
		{"TTY_COMPATIBLE": "0", "FORCE_COLOR": "1"},
		{"TTY_COMPATIBLE": "1", "TERM": "dumb"},
		// A terminal rich calls "dumb" has no colour system even so.
		{"FORCE_COLOR": "1", "TERM": "dumb"},
		{"FORCE_COLOR": "1", "TERM": "unknown"},
		{"FORCE_COLOR": "1", "TERM": "tmux-256color"},
		{"TERM": "dumb"},
	}

	payload, err := json.Marshal(envs)
	if err != nil {
		t.Fatal(err)
	}
	out, err := exec.Command(py, "-c", pyEnabledDriver, string(payload)).Output()
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			t.Fatalf("python driver failed: %v\n%s", err, ee.Stderr)
		}
		t.Fatal(err)
	}
	var want []struct {
		Enabled bool `json:"enabled"`
		NoColor bool `json:"no_color"`
	}
	if err := json.Unmarshal(out, &want); err != nil {
		t.Fatalf("decoding %q: %v", out, err)
	}

	for i, env := range envs {
		for _, k := range []string{"FORCE_COLOR", "NO_COLOR", "TERM", "TTY_COMPATIBLE", "COLORTERM"} {
			t.Setenv(k, "")
			os.Unsetenv(k)
		}
		for k, v := range env {
			t.Setenv(k, v)
		}
		if got := Enabled(); got != want[i].Enabled {
			t.Errorf("env %v: Enabled() = %v, rich = %v", env, got, want[i].Enabled)
		}
		if got := NoColor(); got != want[i].NoColor {
			t.Errorf("env %v: NoColor() = %v, rich = %v", env, got, want[i].NoColor)
		}
	}
}

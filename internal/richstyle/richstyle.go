// Package richstyle renders the small slice of rich's console markup that
// boxyard's Python actually uses, byte-for-byte as rich renders it.
//
// The Python builds strings like "[bold green]Success[/bold green]" and hands
// them to a rich Console. Those bytes are part of the output contract in the
// same way the words are: `boxyard list --view groups` bolds group names and
// dims the group suffix, and `multi-sync` colours each box's outcome. A port
// that printed the words without the escapes would look right in a pipe and
// wrong on a terminal — which is the harder kind of difference to notice,
// because the pipe is what every automated comparison sees.
//
// Only what the Python uses is supported: the attributes `bold` and `dim`, and
// the eight standard colour names. An unknown tag is a LOUD error rather than
// a silently-dropped style, so a new style added on the Python side surfaces
// here instead of quietly rendering as plain text.
package richstyle

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

// attributes are rich's SGR numbers for the text attributes, which it emits
// BEFORE the colour and in this order.
var attributes = map[string]string{
	"bold": "1",
	"dim":  "2",
}

// colours are rich's SGR numbers for the standard colour names. rich emits
// these same codes under every colour system — truecolor, 256, standard and
// windows all render `[green]` as "\x1b[32m", because a named standard colour
// carries its own ANSI number.
var colours = map[string]string{
	"black": "30", "red": "31", "green": "32", "yellow": "33",
	"blue": "34", "magenta": "35", "cyan": "36", "white": "37",
}

// Enabled reports whether rich would emit escapes at all.
//
// A default `rich.Console()` renders a style only when it has a colour system,
// and it has one only when the stream is a terminal AND that terminal is not
// "dumb". So this is the conjunction of rich's `is_terminal` and the negation
// of its `is_dumb_terminal`, not a plain isatty. The precedence inside
// is_terminal is rich's own, and none of it is the obvious rule:
//
//   - TTY_COMPATIBLE is checked FIRST and decides outright, either way;
//   - then FORCE_COLOR, where the value matters — set-but-empty means NO,
//     which is the opposite of how most tools read it (https://force-color.org/);
//   - only then isatty.
//
// Getting this wrong in either direction is visible: too eager and the
// supervisor's log fills with escape sequences; too shy and an interactive run
// silently loses its colour.
func Enabled() bool {
	if !isTerminal() {
		return false
	}
	// rich's is_dumb_terminal.
	switch os.Getenv("TERM") {
	case "dumb", "unknown":
		return false
	}
	return true
}

func isTerminal() bool {
	switch os.Getenv("TTY_COMPATIBLE") {
	case "0":
		return false
	case "1":
		return true
	}
	if v, ok := os.LookupEnv("FORCE_COLOR"); ok {
		return v != ""
	}
	_, err := unix.IoctlGetWinsize(int(os.Stdout.Fd()), unix.TIOCGWINSZ)
	return err == nil
}

// NoColor reports whether rich would drop colours. It keeps the attributes:
// NO_COLOR asks for no colour, not for no formatting, and rich honours that
// distinction — `[bold green]` still renders bold.
//
// Set-but-empty is NOT set, matching rich (and FORCE_COLOR above), so a
// `NO_COLOR=` in the environment does not silently drain the colour out of
// every command.
func NoColor() bool { return os.Getenv("NO_COLOR") != "" }

// tagRe matches a markup tag and any backslashes in front of it. It is rich's
// own RE_TAGS pattern: a tag opens with a lowercase letter, '#', '/' or '@'.
var tagRe = regexp.MustCompile(`(\\*)\[([a-z#/@][^\[]*?)\]`)

// Escape makes a value safe to interpolate into markup, as rich.markup.escape
// does. Box and group names may legally contain brackets — `validate_box_name`
// forbids only path separators and the like — and an unescaped `[archived]` in
// a name would be eaten as a style tag.
func Escape(s string) string {
	out := tagRe.ReplaceAllStringFunc(s, func(m string) string {
		g := tagRe.FindStringSubmatch(m)
		backslashes, tag := g[1], g[2]
		return backslashes + backslashes + `\[` + tag + `]`
	})
	if strings.HasSuffix(out, `\`) && !strings.HasSuffix(out, `\\`) {
		return out + `\`
	}
	return out
}

// segment is a run of text with the set of tags open over it. This is rich's
// own intermediate form, and having it lets the tree renderer wrap the PLAIN
// text and then re-emit the styles over the wrapped lines — which is what rich
// does, and the only way to get a wrapped styled label right. Splitting the
// markup STRING at the break offsets cannot work: a break may land inside an
// escaped bracket, or between a tag and its text.
type segment struct {
	text  string
	stack []string
}

// Segments resolves markup into styled runs.
func Segments(markup string) ([]segment, error) {
	var out []segment
	var stack []string
	var pending strings.Builder

	flush := func() {
		if pending.Len() == 0 {
			return
		}
		out = append(out, segment{text: pending.String(), stack: append([]string{}, stack...)})
		pending.Reset()
	}

	pos := 0
	for _, loc := range tagRe.FindAllStringSubmatchIndex(markup, -1) {
		full, tagStart, tagEnd := loc[0], loc[4], loc[5]
		bsStart, bsEnd := loc[2], loc[3]
		backslashes := markup[bsStart:bsEnd]

		pending.WriteString(unescapeText(markup[pos:full]))
		pos = loc[1]

		// An ODD number of backslashes escapes the tag: it is literal text.
		// rich doubles them when escaping, so `\\[x]` is a real tag preceded
		// by one literal backslash.
		if len(backslashes)%2 == 1 {
			pending.WriteString(strings.Repeat(`\`, (len(backslashes)-1)/2))
			pending.WriteString(markup[bsEnd:loc[1]])
			continue
		}
		pending.WriteString(strings.Repeat(`\`, len(backslashes)/2))

		tag := markup[tagStart:tagEnd]
		flush()

		if strings.HasPrefix(tag, "/") {
			if len(stack) == 0 {
				return nil, fmt.Errorf("richstyle: closing tag [%s] with nothing open in %q", tag, markup)
			}
			closing := strings.TrimSpace(tag[1:])
			if closing != "" && closing != strings.TrimSpace(stack[len(stack)-1]) {
				return nil, fmt.Errorf("richstyle: [%s] does not close [%s] in %q", tag, stack[len(stack)-1], markup)
			}
			stack = stack[:len(stack)-1]
			continue
		}
		if _, err := sgr([]string{tag}, false); err != nil {
			return nil, err
		}
		stack = append(stack, tag)
	}
	pending.WriteString(unescapeText(markup[pos:]))
	flush()
	if len(stack) > 0 {
		return nil, fmt.Errorf("richstyle: unclosed tag [%s] in %q", stack[len(stack)-1], markup)
	}
	return out, nil
}

// Plain is the text of markup with every tag removed.
func Plain(segs []segment) string {
	var b strings.Builder
	for _, s := range segs {
		b.WriteString(s.text)
	}
	return b.String()
}

// RenderSegments writes segments the way rich writes them.
//
// rich does NOT emit one escape per tag. It writes each segment as a
// self-contained "codes … reset" run, so nesting COMBINES styles rather than
// layering them: `[dim]a [bold]b[/bold] c[/dim]` gives three segments, the
// middle one "\x1b[1;2m" — bold and dim together, in rich's attribute order
// rather than the order they were opened. A segment whose composed style has
// no codes is written plain, with no reset, and an EMPTY segment is written
// not at all. Every one of those is a place a per-tag renderer diverges while
// still looking plausible.
func RenderSegments(segs []segment, enable, noColour bool) (string, error) {
	var out strings.Builder
	for _, seg := range segs {
		if seg.text == "" {
			continue
		}
		if !enable {
			out.WriteString(seg.text)
			continue
		}
		code, err := sgr(seg.stack, noColour)
		if err != nil {
			return "", err
		}
		if code == "" {
			out.WriteString(seg.text)
			continue
		}
		out.WriteString(code)
		out.WriteString(seg.text)
		out.WriteString("\x1b[0m")
	}
	return out.String(), nil
}

// Render turns markup into the bytes rich would write.
//
// enable and noColour are passed in rather than read from the environment so
// that the differential can drive every combination without setenv, and so a
// caller can render to a buffer whose destination it already knows.
func Render(markup string, enable, noColour bool) (string, error) {
	segs, err := Segments(markup)
	if err != nil {
		return "", err
	}
	return RenderSegments(segs, enable, noColour)
}

// MustRender is Render for markup this package builds itself, where a parse
// failure is a bug in the caller rather than in the data.
func MustRender(markup string, enable, noColour bool) string {
	s, err := Render(markup, enable, noColour)
	if err != nil {
		panic(err)
	}
	return s
}

// attributeOrder is the order rich emits its attribute codes in, which is the
// order of the bits in its Style, NOT the order the tags were opened.
var attributeOrder = []string{"bold", "dim"}

// sgr composes the open tags into the one escape rich would write for them:
// every attribute any tag sets, in rich's order, then the colour set by the
// INNERMOST tag that sets one.
func sgr(stack []string, noColour bool) (string, error) {
	set := map[string]bool{}
	colour := ""
	for _, tag := range stack {
		for _, word := range strings.Fields(tag) {
			if _, ok := attributes[word]; ok {
				set[word] = true
				continue
			}
			if code, ok := colours[word]; ok {
				colour = code
				continue
			}
			return "", fmt.Errorf("richstyle: unsupported style %q in tag [%s]", word, tag)
		}
	}
	var codes []string
	for _, name := range attributeOrder {
		if set[name] {
			codes = append(codes, attributes[name])
		}
	}
	if colour != "" && !noColour {
		codes = append(codes, colour)
	}
	if len(codes) == 0 {
		return "", nil
	}
	return "\x1b[" + strings.Join(codes, ";") + "m", nil
}

// unescapeText turns rich's `\[` back into a literal bracket.
func unescapeText(s string) string { return strings.ReplaceAll(s, `\[`, "[") }

// ConsoleWidth is the width a default rich Console would wrap to.
//
// It is NOT shutil.get_terminal_size, which is what the rest of boxyard's
// Python uses for its dot padding: rich probes stdIN first and lets COLUMNS
// override whatever it found, where shutil consults COLUMNS first and then
// stdout. The two agree in a terminal and diverge exactly where it matters —
// a piped run with a terminal on stdin.
func ConsoleWidth() int {
	// rich's is_dumb_terminal short-circuits to a fixed 80x25.
	if isTerminal() {
		switch os.Getenv("TERM") {
		case "dumb", "unknown":
			return 80
		}
	}
	width := 0
	for _, fd := range []int{0, 1, 2} {
		if ws, err := unix.IoctlGetWinsize(fd, unix.TIOCGWINSZ); err == nil && ws.Col > 0 {
			width = int(ws.Col)
			break
		}
	}
	if cols := os.Getenv("COLUMNS"); cols != "" && isAllDigits(cols) {
		if n, err := strconv.Atoi(cols); err == nil {
			width = n
		}
	}
	if width <= 0 {
		return 80
	}
	return width
}

func isAllDigits(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return s != ""
}

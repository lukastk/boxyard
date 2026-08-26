package cli

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// Doctor's contract, stated in its own package doc, is that every hint names an
// exact command that is safe to run verbatim. Two of them did not parse: the
// diverged-box hint said `--sync-direction to_remote` (that is the sync
// RECORD's direction name; the flag takes push/pull), and a local check said
// `boxyard new --from` with no argument. Both were found the hard way, by
// trying to follow the advice on real wedged boxes.
//
// A hint is a string literal, so nothing type-checks it. This walks doctor's
// source, pulls every `boxyard ...` out of its literals, and feeds each one to
// the real cobra parser. The Python side has the same lint over the same
// hints (src/tests/unit/cmds/test_doctor_hints_are_runnable.py) — the two
// implementations' hint text is meant to stay identical, so the checks are too.

// placeholder stands in for a Sprintf verb or a non-constant concat operand.
// It has to be a single shell word with no metacharacters: it takes the place
// of a real value in the parsed command.
const placeholder = "PLACEHOLDER"

var (
	commandRe = regexp.MustCompile("`(boxyard [^`]*)`")
	verbRe    = regexp.MustCompile(`%[-+# 0-9.*\[\]]*[a-zA-Z]`)
	// A hint often names a bare subcommand as prose ("via `boxyard new`") — a
	// template, not something to run. Those are exempt from needing arguments,
	// but they must still name a command that EXISTS.
	bareRe = regexp.MustCompile(`^boxyard [a-z-]+$`)
)

// literalStrings returns every string literal in the file with format verbs
// and non-constant concat operands replaced by a placeholder.
//
// Go's parser does NOT join adjacent string literals the way Python's does —
// a hint split over several lines is a BinaryExpr tree of `+`. So this walks
// those trees and flattens them, which is what makes a wrapped hint arrive
// here as one command rather than several unparseable fragments.
func literalStrings(f *ast.File) []string {
	var out []string
	seen := map[ast.Expr]bool{}

	// flatten renders an expression as text, or reports false if it is not
	// string-shaped at all.
	var flatten func(ast.Expr) (string, bool)
	flatten = func(e ast.Expr) (string, bool) {
		switch v := e.(type) {
		case *ast.BasicLit:
			if v.Kind != token.STRING {
				return "", false
			}
			s, err := strconv.Unquote(v.Value)
			if err != nil {
				return "", false
			}
			return s, true
		case *ast.BinaryExpr:
			if v.Op != token.ADD {
				return "", false
			}
			l, lok := flatten(v.X)
			r, rok := flatten(v.Y)
			if !lok && !rok {
				return "", false
			}
			if !lok {
				l = placeholder
			}
			if !rok {
				r = placeholder
			}
			return l + r, true
		default:
			return "", false
		}
	}

	ast.Inspect(f, func(n ast.Node) bool {
		e, ok := n.(ast.Expr)
		if !ok || seen[e] {
			return true
		}
		s, ok := flatten(e)
		if !ok {
			return true
		}
		// Mark the whole subtree consumed so the halves of a concat are not
		// also emitted on their own.
		ast.Inspect(e, func(m ast.Node) bool {
			if me, ok := m.(ast.Expr); ok {
				seen[me] = true
			}
			return true
		})
		out = append(out, verbRe.ReplaceAllString(s, placeholder))
		return true
	})
	return out
}

func suggestedCommands(t *testing.T) []string {
	t.Helper()
	dir := filepath.Join("..", "doctor")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("reading doctor package: %v", err)
	}
	fset := token.NewFileSet()
	var cmds []string
	seen := map[string]bool{}
	for _, e := range entries {
		if !strings.HasSuffix(e.Name(), ".go") || strings.HasSuffix(e.Name(), "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, filepath.Join(dir, e.Name()), nil, 0)
		if err != nil {
			t.Fatalf("parsing %s: %v", e.Name(), err)
		}
		for _, lit := range literalStrings(f) {
			for _, m := range commandRe.FindAllStringSubmatch(lit, -1) {
				cmd := strings.Join(strings.Fields(m[1]), " ")
				if !seen[cmd] {
					seen[cmd] = true
					cmds = append(cmds, cmd)
				}
			}
		}
	}
	if len(cmds) == 0 {
		t.Fatal("found no `boxyard ...` commands in doctor's source — the extraction is broken, not the hints")
	}
	return cmds
}

// splitWords is a shell-style split that only has to handle what a hint
// contains: single-quoted values (doctor quotes every interpolated path and
// index name) and plain words.
func splitWords(s string) ([]string, error) {
	var words []string
	var cur strings.Builder
	inWord, inQuote := false, false
	for _, r := range s {
		switch {
		case r == '\'':
			inQuote = !inQuote
			inWord = true
		case !inQuote && (r == ' ' || r == '\t'):
			if inWord {
				words = append(words, cur.String())
				cur.Reset()
				inWord = false
			}
		default:
			cur.WriteRune(r)
			inWord = true
		}
	}
	if inQuote {
		return nil, errUnbalancedQuote
	}
	if inWord {
		words = append(words, cur.String())
	}
	return words, nil
}

var errUnbalancedQuote = strconv.ErrSyntax

func TestDoctorSuggestedCommandsParse(t *testing.T) {
	for _, cmd := range suggestedCommands(t) {
		t.Run(cmd, func(t *testing.T) {
			argv, err := splitWords(cmd)
			if err != nil {
				t.Fatalf("`%s` has an unbalanced quote", cmd)
			}
			argv = argv[1:] // drop "boxyard"

			root := NewRootCommand()
			sub, _, err := root.Find(argv)
			if err != nil || sub == nil || sub == root {
				t.Fatalf("`%s` names a subcommand that does not exist", cmd)
			}
			if bareRe.MatchString(cmd) {
				return // prose mention of a command, not an invocation
			}
			// Parse the flags the way cobra will at run time. Missing
			// positional arguments are NOT an error here: a hint may name a
			// command whose argument the user supplies.
			flagArgs := argv[len(strings.Fields(sub.CommandPath()))-1:]
			if err := parseFlags(sub, flagArgs); err != nil {
				// A hint may interpolate a value into an ENUM option
				// (`--sync-choices <part>`). That value is the operator's,
				// not the hint's, so a placeholder failing the enum check
				// says nothing — but a LITERAL failing it is exactly the bug
				// this test exists for (`--sync-direction to_remote`), and
				// pflag's message quotes the literal.
				if strings.Contains(err.Error(), `"`+placeholder+`"`) {
					return
				}
				t.Fatalf("`%s` does not parse: %v", cmd, err)
			}
		})
	}
}

// parseFlags runs the command's flag parsing without running the command.
func parseFlags(cmd *cobra.Command, args []string) error {
	cmd.InitDefaultHelpFlag()
	return cmd.Flags().Parse(args)
}

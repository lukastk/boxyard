package boxref

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// FZF drives the fzf command-line fuzzy finder.
//
// fzf renders its UI on /dev/tty, so stdout stays free for the chosen line —
// which is why stdout is captured while stderr is passed straight through.
type FZF struct {
	// Binary defaults to "fzf".
	Binary string
	// Multi selects fzf's --multi mode (Tab/Space to toggle, Enter to confirm).
	Multi bool
}

// Pick shows dispTerms and returns the parallel entry from terms.
func (f FZF) Pick(terms, dispTerms []string) (int, string, error) {
	selections, err := f.PickMulti(terms, dispTerms)
	if err != nil {
		return 0, "", err
	}
	if len(selections) == 0 {
		return 0, "", ErrPickCancelled
	}
	return selections[0].Index, selections[0].Term, nil
}

// Selection is one chosen entry.
type Selection struct {
	Index int
	Term  string
}

// PickMulti returns every chosen entry. With Multi false there is at most one.
func (f FZF) PickMulti(terms, dispTerms []string) ([]Selection, error) {
	if dispTerms == nil {
		dispTerms = terms
	}
	if err := rejectAmbiguous(terms, dispTerms); err != nil {
		return nil, err
	}

	binary := f.Binary
	if binary == "" {
		binary = "fzf"
	}
	argv := []string{binary}
	if f.Multi {
		argv = append(argv, "--multi")
	}

	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdin = strings.NewReader(strings.Join(dispTerms, "\n"))
	var stdout strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			// fzf exits 1 for "no match" and 130 for an interrupt. Both mean
			// "nothing was chosen", which is not a failure.
			return nil, nil
		}
		if errors.Is(err, exec.ErrNotFound) {
			return nil, errors.New("fzf is not installed or not found in PATH.")
		}
		return nil, err
	}

	index := map[string]int{}
	for i, term := range dispTerms {
		index[strings.TrimSpace(term)] = i
	}
	var out []Selection
	for _, line := range strings.Split(strings.TrimSpace(stdout.String()), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		i, ok := index[line]
		if !ok {
			// fzf can only return a line it was given, so this means the two
			// lists went out of step — a bug, not a user action.
			return nil, fmt.Errorf("fzf returned a line that was not offered: %q", line)
		}
		out = append(out, Selection{Index: i, Term: terms[i]})
	}
	return out, nil
}

// rejectAmbiguous refuses a picker whose display lines cannot be mapped back to
// one term.
//
// fzf returns the LINE the user chose, and the mapping back is by display
// string. Two items rendering to the same line would resolve to the same term,
// so picking the second one silently acts on the first — and these pickers feed
// `boxyard exclude`, so that is the wrong box removed from the machine. Every
// caller embeds the box id in its display line, so this cannot fire; it exists
// so a caller that forgets gets a loud error instead of a wrong box.
//
// Mismatched lengths are refused for the same reason: the mapping is
// positional. (Python gained the same guard in v0.5.6.)
func rejectAmbiguous(terms, dispTerms []string) error {
	if len(terms) != len(dispTerms) {
		return fmt.Errorf("fzf: %d terms but %d display terms; the two are mapped positionally and must be the same length",
			len(terms), len(dispTerms))
	}
	seen := map[string]bool{}
	for _, term := range dispTerms {
		key := strings.TrimSpace(term)
		if seen[key] {
			return fmt.Errorf("fzf: duplicate display term %q. fzf returns the chosen LINE, so duplicates cannot be mapped back to a single item and picking one would act on another. Include something unique (the box id) in each display term", key)
		}
		seen[key] = true
	}
	return nil
}

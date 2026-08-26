package cli

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/boxref"
	"github.com/lukastk/boxyard/internal/locking"
	"github.com/lukastk/boxyard/internal/models"
)

// The `--interactive` pickers on `include` and `exclude` are fzf multi-selects:
// pick any number of boxes, confirm the list, then act on them one at a time.
// They are the surface actually in daily use (the Textual TUI that `path -I`
// used to open is gone), so their output is a contract like any other — the
// glyphs, the size column and the "[i/n]" progress lines are reproduced exactly
// as the Python prints them.
//
// A per-box failure does NOT abort the run: the error is reported and collected
// and the remaining boxes are still processed, because a batch of twenty
// exclusions should not be halted by one bad box. A LOCK failure is the
// exception — it means another boxyard is working in this yard right now, so
// every subsequent box would fail the same way.

// pickCandidate is one box offered in an interactive picker.
type pickCandidate struct {
	bm *models.BoxMeta
	// size is the local DATA size in bytes; only computed for `exclude
	// --show-sizes`.
	size int64
}

// interactivePick runs the fzf multi-select and the confirmation prompt.
//
// It returns the chosen candidates, or nil if there was nothing to offer, the
// user chose nothing, or the user declined the confirmation — all three are
// ordinary outcomes that leave the command a no-op.
func interactivePick(
	candidates []pickCandidate,
	dispTerm func(pickCandidate) string,
	emptyMsg, verbNoun string,
	confirmLine func(pickCandidate) string,
	totalLine func([]pickCandidate) string,
	out io.Writer,
	in io.Reader,
) ([]pickCandidate, error) {
	if len(candidates) == 0 {
		fmt.Fprintln(out, emptyMsg)
		return nil, nil
	}

	terms := make([]string, len(candidates))
	dispTerms := make([]string, len(candidates))
	for i, c := range candidates {
		terms[i] = c.bm.IndexName()
		dispTerms[i] = dispTerm(c)
	}

	selections, err := (boxref.FZF{Multi: true}).PickMulti(terms, dispTerms)
	if err != nil {
		return nil, err
	}
	if len(selections) == 0 {
		fmt.Fprintln(out, "No boxes selected.")
		return nil, nil
	}

	chosen := make([]pickCandidate, len(selections))
	for i, s := range selections {
		chosen[i] = candidates[s.Index]
	}

	fmt.Fprintf(out, "Boxes to %s:\n", verbNoun)
	for _, c := range chosen {
		fmt.Fprintln(out, confirmLine(c))
	}
	if totalLine != nil {
		if line := totalLine(chosen); line != "" {
			fmt.Fprintln(out, line)
		}
	}
	ok, err := confirm(fmt.Sprintf("%s %d box(es)?", strings.ToUpper(verbNoun[:1])+verbNoun[1:], len(chosen)), out, in)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, nil
	}
	return chosen, nil
}

// errAborted is what click raises on EOF or Ctrl-C at a prompt. click prints
// "Aborted!" and exits 1; Execute prints "Error: <err>" and exits 1, so the
// message is not byte-identical, but the outcome is: nothing was done.
var errAborted = errors.New("Aborted!")

// confirm reproduces click.confirm's default-no prompt: it re-asks on anything
// it does not understand, and treats a bare Enter as "no". It deliberately
// does NOT default on EOF — click aborts there, because a prompt nobody can
// answer must not be answered on their behalf.
func confirm(prompt string, out io.Writer, in io.Reader) (bool, error) {
	reader := bufio.NewReader(in)
	for {
		fmt.Fprintf(out, "%s [y/N]: ", prompt)
		line, err := reader.ReadString('\n')
		if err != nil {
			if !errors.Is(err, io.EOF) {
				return false, err
			}
			if strings.TrimSpace(line) == "" {
				return false, errAborted
			}
			// A final line with no trailing newline is still an answer.
		}
		switch strings.ToLower(strings.TrimSpace(line)) {
		case "y", "yes":
			return true, nil
		case "n", "no", "":
			return false, nil
		default:
			fmt.Fprintln(out, "Error: invalid input")
			if errors.Is(err, io.EOF) {
				return false, errAborted
			}
		}
	}
}

// runBatch applies act to each chosen box, printing the Python's progress
// lines and collecting per-box failures.
func runBatch(chosen []pickCandidate, gerund string, act func(*models.BoxMeta) error, out, errOut io.Writer) error {
	type failure struct{ name, err string }
	var failures []failure

	for i, c := range chosen {
		fmt.Fprintf(out, "[%d/%d] %s %s (%s)...\n", i+1, len(chosen), gerund, c.bm.Name, c.bm.BoxID())
		if err := act(c.bm); err != nil {
			// A lock failure means another boxyard holds this yard; every
			// remaining box would hit the same wall, so stop.
			var lockErr *locking.AcquisitionError
			if errors.As(err, &lockErr) {
				return err
			}
			fmt.Fprintf(errOut, "  Error: %s\n", err)
			failures = append(failures, failure{c.bm.Name, err.Error()})
		}
	}

	if len(failures) > 0 {
		fmt.Fprintf(out, "\n%d error(s):\n", len(failures))
		for _, f := range failures {
			fmt.Fprintf(errOut, "  %s: %s\n", f.name, f.err)
		}
	}
	return nil
}

// The four line formats below are the pickers' entire visible contract, and
// they live here rather than inline at the call sites so the differential
// against the Python (interactive_pick_differential_test.go) exercises the
// same code the commands do. Asserting a second copy of a format string would
// pass whatever the commands actually printed.

// includePickLine renders one fzf line of `include -I`. The hollow circle
// marks a box that is NOT on this machine.
func includePickLine(bm *models.BoxMeta) string {
	return fmt.Sprintf("\u25cb %s (%s)%s", bm.Name, bm.BoxID(), groupTag(bm))
}

// excludePickLine renders one fzf line of `exclude -I`. The filled circle
// marks a box that IS on this machine. sizePrefix is empty without
// --show-sizes.
func excludePickLine(bm *models.BoxMeta, sizePrefix string) string {
	return fmt.Sprintf("%s\u25cf %s (%s)%s", sizePrefix, bm.Name, bm.BoxID(), groupTag(bm))
}

// includeConfirmLine renders one line of the list shown before confirming.
func includeConfirmLine(bm *models.BoxMeta) string {
	return fmt.Sprintf("  %s (%s)", bm.Name, bm.BoxID())
}

// excludeConfirmLine is includeConfirmLine with the optional size column.
func excludeConfirmLine(bm *models.BoxMeta, sizePrefix string) string {
	return fmt.Sprintf("  %s%s (%s)", sizePrefix, bm.Name, bm.BoxID())
}

// groupTag renders a box's groups the way the pickers' display lines do.
func groupTag(bm *models.BoxMeta) string {
	if len(bm.Groups) == 0 {
		return ""
	}
	return " [" + strings.Join(bm.Groups, ", ") + "]"
}

// formatSize renders a byte count the way `exclude --show-sizes` does: GB to
// one decimal place, or whole MB below 0.1 GB.
func formatSize(b int64) string {
	gb := float64(b) / (1024 * 1024 * 1024)
	if gb >= 0.1 {
		return fmt.Sprintf("%.1f GB", gb)
	}
	return fmt.Sprintf("%.0f MB", float64(b)/(1024*1024))
}

// dirSize totals the sizes of the regular files under root.
//
// Unreadable entries are skipped rather than failing the whole picker: this
// number decides only the sort order and a label, and a box the user cannot
// fully stat is still a box they may want to exclude. It follows symlinks when
// sizing a file (matching os.path.getsize) but does not descend through them
// (matching os.walk), so a link into a huge tree cannot be counted twice.
func dirSize(root string) int64 {
	var total int64
	_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil //nolint:nilerr // an unreadable subtree contributes 0
		}
		if d.IsDir() {
			return nil
		}
		info, err := os.Stat(path)
		if err != nil {
			return nil
		}
		if info.IsDir() {
			return nil
		}
		total += info.Size()
		return nil
	})
	return total
}

// sortByName orders candidates the way the Python's `sort(key=lambda bm:
// bm.name)` does — a plain code-point comparison, stable.
func sortByName(candidates []pickCandidate) {
	sort.SliceStable(candidates, func(i, j int) bool {
		return candidates[i].bm.Name < candidates[j].bm.Name
	})
}

// sortBySizeDesc orders candidates largest-first, stably.
func sortBySizeDesc(candidates []pickCandidate) {
	sort.SliceStable(candidates, func(i, j int) bool {
		return candidates[i].size > candidates[j].size
	})
}

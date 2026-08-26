package cli

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/locking"
	"github.com/lukastk/boxyard/internal/models"
)

func TestFormatSize(t *testing.T) {
	const gb = 1024 * 1024 * 1024
	const mb = 1024 * 1024
	cases := []struct {
		bytes int64
		want  string
	}{
		{0, "0 MB"},
		{mb, "1 MB"},
		// Below 0.1 GB the label is whole MB; at 0.1 GB it flips to GB. The
		// boundary is the one place the two branches could disagree.
		{gb / 10, "102 MB"},
		{gb/10 + 1, "0.1 GB"},
		{100 * mb, "100 MB"},
		{150 * mb, "0.1 GB"},
		{gb, "1.0 GB"},
		{3*gb + gb/2, "3.5 GB"},
		{1536 * mb, "1.5 GB"},
	}
	for _, c := range cases {
		if got := formatSize(c.bytes); got != c.want {
			t.Errorf("formatSize(%d) = %q, want %q", c.bytes, got, c.want)
		}
	}
}

func TestGroupTag(t *testing.T) {
	if got := groupTag(&models.BoxMeta{}); got != "" {
		t.Errorf("no groups: got %q, want empty", got)
	}
	bm := &models.BoxMeta{Groups: []string{"work", "ctx/mac"}}
	if got := groupTag(bm); got != " [work, ctx/mac]" {
		t.Errorf("got %q", got)
	}
}

func candidate(name string, size int64) pickCandidate {
	return pickCandidate{bm: &models.BoxMeta{Name: name}, size: size}
}

func names(cs []pickCandidate) []string {
	out := make([]string, len(cs))
	for i, c := range cs {
		out[i] = c.bm.Name
	}
	return out
}

func TestSortByName(t *testing.T) {
	cs := []pickCandidate{candidate("zeta", 0), candidate("Alpha", 0), candidate("beta", 0)}
	sortByName(cs)
	// A plain code-point comparison, so uppercase sorts before lowercase —
	// the same order the Python's sort(key=lambda bm: bm.name) produces.
	if got := strings.Join(names(cs), ","); got != "Alpha,beta,zeta" {
		t.Errorf("got %s", got)
	}
}

func TestSortBySizeDescIsStable(t *testing.T) {
	cs := []pickCandidate{
		candidate("first-tie", 100), candidate("big", 900),
		candidate("second-tie", 100), candidate("small", 1),
	}
	sortBySizeDesc(cs)
	// Equal sizes keep their original relative order, matching Python's
	// stable sort(..., reverse=True). Without that a --show-sizes picker
	// would reshuffle equal-size boxes between runs.
	if got := strings.Join(names(cs), ","); got != "big,first-tie,second-tie,small" {
		t.Errorf("got %s", got)
	}
}

func TestConfirm(t *testing.T) {
	cases := []struct {
		input   string
		want    bool
		wantErr error
	}{
		{"y\n", true, nil},
		{"Y\n", true, nil},
		{"yes\n", true, nil},
		{"  yes  \n", true, nil},
		{"n\n", false, nil},
		{"no\n", false, nil},
		{"\n", false, nil}, // bare Enter takes the default, which is no
		{"maybe\ny\n", true, nil},
		{"y", true, nil}, // a final answer with no trailing newline
		{"", false, errAborted},
		{"maybe\n", false, errAborted}, // ran out of input mid-prompt
	}
	for _, c := range cases {
		var out bytes.Buffer
		got, err := confirm("Include 2 box(es)?", &out, strings.NewReader(c.input))
		if !errors.Is(err, c.wantErr) {
			t.Errorf("confirm(%q) err = %v, want %v", c.input, err, c.wantErr)
			continue
		}
		if got != c.want {
			t.Errorf("confirm(%q) = %v, want %v", c.input, got, c.want)
		}
		if !strings.HasPrefix(out.String(), "Include 2 box(es)? [y/N]: ") {
			t.Errorf("confirm(%q) prompt = %q", c.input, out.String())
		}
	}
}

func TestConfirmRepromptsOnGarbage(t *testing.T) {
	var out bytes.Buffer
	if _, err := confirm("Go?", &out, strings.NewReader("what\ny\n")); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Error: invalid input") {
		t.Errorf("no complaint about the bad answer: %q", out.String())
	}
	if strings.Count(out.String(), "Go? [y/N]: ") != 2 {
		t.Errorf("expected two prompts, got %q", out.String())
	}
}

func TestRunBatchReportsEachFailureAndKeepsGoing(t *testing.T) {
	chosen := []pickCandidate{
		{bm: &models.BoxMeta{Name: "ok-one", CreationTimestampUTC: "20250101_000000", BoxSubid: "aaaaa"}},
		{bm: &models.BoxMeta{Name: "bad", CreationTimestampUTC: "20250101_000000", BoxSubid: "bbbbb"}},
		{bm: &models.BoxMeta{Name: "ok-two", CreationTimestampUTC: "20250101_000000", BoxSubid: "ccccc"}},
	}
	var out, errOut bytes.Buffer
	var seen []string
	err := runBatch(chosen, "Excluding", func(bm *models.BoxMeta) error {
		seen = append(seen, bm.Name)
		if bm.Name == "bad" {
			return fmt.Errorf("no good")
		}
		return nil
	}, &out, &errOut)
	if err != nil {
		t.Fatalf("a per-box failure must not fail the batch: %v", err)
	}
	if strings.Join(seen, ",") != "ok-one,bad,ok-two" {
		t.Errorf("the box after the failure was skipped: %v", seen)
	}
	if !strings.Contains(out.String(), "[2/3] Excluding bad (20250101_000000_bbbbb)...") {
		t.Errorf("progress line missing: %q", out.String())
	}
	if !strings.Contains(out.String(), "\n1 error(s):\n") {
		t.Errorf("error summary missing: %q", out.String())
	}
	if !strings.Contains(errOut.String(), "  Error: no good\n") ||
		!strings.Contains(errOut.String(), "  bad: no good\n") {
		t.Errorf("stderr = %q", errOut.String())
	}
}

func TestRunBatchAbortsOnLockFailure(t *testing.T) {
	chosen := []pickCandidate{
		{bm: &models.BoxMeta{Name: "one"}},
		{bm: &models.BoxMeta{Name: "two"}},
	}
	var out, errOut bytes.Buffer
	var seen int
	lockErr := &locking.AcquisitionError{LockType: "global", LockPath: "/x", Err: errors.New("busy")}
	err := runBatch(chosen, "Including", func(bm *models.BoxMeta) error {
		seen++
		return lockErr
	}, &out, &errOut)
	// Another boxyard holds the yard, so every remaining box would fail the
	// same way. Grinding through them would print one failure per box and
	// leave the operator to work out they were all the same thing.
	if !errors.Is(err, lockErr) {
		t.Fatalf("lock failure must abort the batch, got %v", err)
	}
	if seen != 1 {
		t.Errorf("kept going after the lock failure: tried %d boxes", seen)
	}
}

func TestInteractivePickWithNothingToOffer(t *testing.T) {
	var out bytes.Buffer
	chosen, err := interactivePick(nil, nil, "No excluded boxes to include.", "include", nil, nil, &out, strings.NewReader(""))
	if err != nil || chosen != nil {
		t.Fatalf("got %v, %v", chosen, err)
	}
	if out.String() != "No excluded boxes to include.\n" {
		t.Errorf("got %q", out.String())
	}
}

func TestDirSizeCountsFilesAndSurvivesUnreadableEntries(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "a"), make([]byte, 10), 0o644); err != nil {
		t.Fatal(err)
	}
	sub := filepath.Join(root, "sub")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sub, "b"), make([]byte, 25), 0o644); err != nil {
		t.Fatal(err)
	}
	// A broken symlink has no size to add and must not abort the walk — this
	// number only picks a sort order and a label.
	if err := os.Symlink(filepath.Join(root, "gone"), filepath.Join(root, "dangling")); err != nil {
		t.Fatal(err)
	}
	if got := dirSize(root); got != 35 {
		t.Errorf("dirSize = %d, want 35", got)
	}
	if got := dirSize(filepath.Join(root, "does-not-exist")); got != 0 {
		t.Errorf("missing dir: got %d, want 0", got)
	}
}

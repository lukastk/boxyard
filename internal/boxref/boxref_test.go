package boxref

import (
	"errors"
	"strings"
	"testing"

	"github.com/lukastk/boxyard/internal/models"
)

func bx(ts, subid, name string, groups ...string) *models.BoxMeta {
	if groups == nil {
		groups = []string{}
	}
	return &models.BoxMeta{
		CreationTimestampUTC: ts, BoxSubid: subid, Name: name, Groups: groups,
	}
}

func yard() []*models.BoxMeta {
	return []*models.BoxMeta{
		bx("20240102", "aaaaa", "boxyard"),
		bx("20240103", "bbbbb", "boxyard-go"),
		bx("20240104", "ccccc", "Sesh"),
		bx("20240105", "ddddd", "myrig"),
	}
}

// recordingPicker returns a fixed choice and remembers what it was offered.
type recordingPicker struct {
	choose    int
	cancel    bool
	gotTerms  []string
	gotDisp   []string
	callCount int
}

func (p *recordingPicker) Pick(terms, disp []string) (int, string, error) {
	p.callCount++
	p.gotTerms, p.gotDisp = terms, disp
	if p.cancel {
		return 0, "", ErrPickCancelled
	}
	return p.choose, terms[p.choose], nil
}

func TestResolveBySelector(t *testing.T) {
	cases := []struct {
		name string
		opts Options
		want string
	}{
		{"by index name", Options{BoxIndexName: "20240102_aaaaa__boxyard"}, "20240102_aaaaa__boxyard"},
		{"by box id", Options{BoxID: "20240103_bbbbb"}, "20240103_bbbbb__boxyard-go"},
		// CONTAINS is the default mode, and it is case-INSENSITIVE by default.
		{"by name, exact match wins outright", Options{BoxName: "myrig"}, "20240105_ddddd__myrig"},
		{"by name, case-insensitive", Options{BoxName: "sesh"}, "20240104_ccccc__Sesh"},
		{"by name, exact mode", Options{BoxName: "boxyard", MatchMode: MatchExact}, "20240102_aaaaa__boxyard"},
		{"by name, subsequence", Options{BoxName: "myg", MatchMode: MatchSubsequence}, "20240105_ddddd__myrig"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Resolve(yard(), nil, tc.opts)
			if err != nil {
				t.Fatal(err)
			}
			if got != tc.want {
				t.Fatalf("got %q, want %q", got, tc.want)
			}
		})
	}
}

func TestResolveRejectsBadCombinations(t *testing.T) {
	cases := []struct {
		name string
		opts Options
		want string
	}{
		{"two selectors", Options{BoxName: "a", BoxID: "b"}, "more than one"},
		{"match mode without a name", Options{BoxID: "x", MatchMode: MatchExact}, "`box-name` must be provided"},
		{"pick-first without a name", Options{BoxID: "x", PickFirst: true}, "`box-name` must be provided"},
		{"unknown match mode", Options{BoxName: "a", MatchMode: "fuzzy"}, "invalid name match mode"},
		// Commands that destroy something set AllowNoArgs false, so a bare
		// `boxyard delete` refuses rather than offering a box to destroy.
		{"no args where they are forbidden", Options{Label: "box"}, "No box name, id or index name provided."},
		{"no args, custom label", Options{Label: "parent"}, "No parent name, id or index name provided."},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Resolve(yard(), nil, tc.opts)
			if err == nil {
				t.Fatal("expected an error")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not mention %q", err, tc.want)
			}
		})
	}
}

func TestResolveUnknownID(t *testing.T) {
	_, err := Resolve(yard(), nil, Options{BoxID: "20991231_zzzzz"})
	if err == nil || !strings.Contains(err.Error(), "not found") {
		t.Fatalf("want a not-found error, got %v", err)
	}
}

func TestResolveNoMatch(t *testing.T) {
	_, err := Resolve(yard(), nil, Options{BoxName: "nothing-like-this"})
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

func TestResolveSeveralMatchesGoToThePicker(t *testing.T) {
	p := &recordingPicker{choose: 1}
	got, err := Resolve(yard(), p, Options{BoxName: "boxyard"})
	if err != nil {
		t.Fatal(err)
	}
	// Candidates are sorted by index name, so "boxyard" precedes "boxyard-go".
	if got != "20240103_bbbbb__boxyard-go" {
		t.Fatalf("got %q", got)
	}
	if len(p.gotTerms) != 2 {
		t.Fatalf("the picker was offered %d terms, want 2", len(p.gotTerms))
	}
	// The display line must carry the box id: it is what makes the lines
	// unique, and the mapping back is by display line.
	for _, line := range p.gotDisp {
		if !strings.Contains(line, "_") || !strings.Contains(line, "(") {
			t.Errorf("display line lacks the box id: %q", line)
		}
	}
}

func TestResolvePickFirstSkipsThePicker(t *testing.T) {
	p := &recordingPicker{choose: 1}
	got, err := Resolve(yard(), p, Options{BoxName: "boxyard", PickFirst: true})
	if err != nil {
		t.Fatal(err)
	}
	if got != "20240102_aaaaa__boxyard" {
		t.Fatalf("got %q, want the first in index-name order", got)
	}
	if p.callCount != 0 {
		t.Fatal("--pick-first must not open a picker")
	}
}

// Dismissing the picker is not a failure — the CLI exits 0.
func TestResolveCancelledPicker(t *testing.T) {
	p := &recordingPicker{cancel: true}
	_, err := Resolve(yard(), p, Options{BoxName: "boxyard"})
	if !errors.Is(err, ErrPickCancelled) {
		t.Fatalf("want ErrPickCancelled, got %v", err)
	}
}

// With no selector at all, every box is a candidate.
func TestResolveNoArgsOffersEveryBox(t *testing.T) {
	p := &recordingPicker{choose: 0}
	got, err := Resolve(yard(), p, Options{AllowNoArgs: true})
	if err != nil {
		t.Fatal(err)
	}
	if len(p.gotTerms) != 4 {
		t.Fatalf("the picker was offered %d boxes, want the whole yard", len(p.gotTerms))
	}
	if got != "20240102_aaaaa__boxyard" {
		t.Fatalf("got %q", got)
	}
}

// A single candidate needs no picker, and must not open one.
func TestResolveSingleCandidateNeedsNoPicker(t *testing.T) {
	p := &recordingPicker{}
	got, err := Resolve(yard(), p, Options{BoxName: "myrig"})
	if err != nil {
		t.Fatal(err)
	}
	if got != "20240105_ddddd__myrig" || p.callCount != 0 {
		t.Fatalf("got %q with %d picker calls", got, p.callCount)
	}
}

// Without a picker, an ambiguous selection must fail LOUDLY rather than
// silently taking the first match.
func TestResolveAmbiguousWithoutAPickerFails(t *testing.T) {
	_, err := Resolve(yard(), nil, Options{BoxName: "boxyard"})
	if err == nil || !strings.Contains(err.Error(), "--pick-first") {
		t.Fatalf("want a loud ambiguity error naming the way out, got %v", err)
	}
}

func TestIsSubsequenceMatch(t *testing.T) {
	cases := []struct {
		term, name string
		want       bool
	}{
		{"lukas", "lukastk", true},
		{"lukas", "I am lukastk", true},
		{"ad", "abcd", true},
		{"acbd", "abcd", false},
		// Python's loop leaves j == 0 == len(term) for an empty term, so it
		// matches everything — and the no-name path relies on that.
		{"", "anything", true},
		{"abc", "", false},
		// Compared over runes: a multi-byte name must not be matched a byte at
		// a time.
		{"åé", "xåyéz", true},
		{"éå", "xåyéz", false},
	}
	for _, tc := range cases {
		if got := IsSubsequenceMatch(tc.term, tc.name); got != tc.want {
			t.Errorf("IsSubsequenceMatch(%q, %q) = %v, want %v", tc.term, tc.name, got, tc.want)
		}
	}
}

func TestRejectAmbiguousDisplayTerms(t *testing.T) {
	if err := rejectAmbiguous([]string{"a", "b"}, []string{"x (1)", "x (2)"}); err != nil {
		t.Fatalf("unique display terms were refused: %v", err)
	}
	if err := rejectAmbiguous([]string{"a", "b"}, []string{"same", "same"}); err == nil {
		t.Fatal("duplicate display terms were accepted")
	}
	// The lookup strips, so the check must strip too.
	if err := rejectAmbiguous([]string{"a", "b"}, []string{"same", "  same  "}); err == nil {
		t.Fatal("duplicates that differ only in whitespace were accepted")
	}
	if err := rejectAmbiguous([]string{"a"}, []string{"x", "y"}); err == nil {
		t.Fatal("mismatched lengths were accepted")
	}
}

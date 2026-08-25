// Package boxref resolves "which box did the user mean?" from the selectors
// the CLI accepts: an index name, a box id, a name (with a match mode), or
// nothing at all.
//
// Ported from `_get_box_index_name` in pts/mod/_cli/main.pct.py. Roughly
// fifteen commands share it, so its rules ARE the CLI's addressing contract.
package boxref

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/lukastk/boxyard/internal/models"
)

// MatchMode selects how a --box-name is compared against box names.
type MatchMode string

const (
	// MatchExact requires the whole name to be equal.
	MatchExact MatchMode = "exact"
	// MatchContains is the DEFAULT when a name is given without a mode.
	MatchContains MatchMode = "contains"
	// MatchSubsequence matches the term's characters in order but not
	// necessarily adjacent — "ad" matches "abcd".
	MatchSubsequence MatchMode = "subsequence"
)

// Valid reports whether m is one of the three modes.
func (m MatchMode) Valid() bool {
	return m == MatchExact || m == MatchContains || m == MatchSubsequence
}

// ErrPickCancelled means the user dismissed the picker without choosing. It is
// not a failure: the CLI exits 0, because "I changed my mind" is not an error.
var ErrPickCancelled = errors.New("no box selected")

// ErrNotFound means nothing matched. The CLI exits 1.
var ErrNotFound = errors.New("Box not found.")

// Picker chooses one of several candidates interactively.
//
// terms and dispTerms are parallel: dispTerms is what the user sees, terms is
// what the caller gets back. An implementation must return ErrPickCancelled
// when nothing was chosen.
type Picker interface {
	Pick(terms, dispTerms []string) (index int, term string, err error)
}

// Options are the selectors a command passes through from its flags.
type Options struct {
	// At most ONE of these three may be set.
	BoxName      string
	BoxID        string
	BoxIndexName string

	// MatchMode requires BoxName. Empty means MatchContains when a name is
	// given.
	MatchMode MatchMode
	// MatchCase compares case-sensitively. The default is insensitive.
	MatchCase bool

	// PickFirst takes the first match instead of showing a picker. It requires
	// BoxName.
	PickFirst bool

	// AllowNoArgs permits a bare invocation, which resolves through the picker.
	// Commands that destroy something (delete, rename, copy, force-push,
	// sync-name) set this false, so a bare `boxyard delete` refuses rather than
	// offering to pick a box to destroy.
	AllowNoArgs bool

	// Label names the thing being selected in the no-args error ("box",
	// "parent").
	Label string
}

// Resolve returns the index name of the box the options select.
//
// Candidates come from `metas`; a command that has already filtered the yard
// (to the excluded boxes, say) passes its own subset.
func Resolve(metas []*models.BoxMeta, p Picker, opts Options) (string, error) {
	label := opts.Label
	if label == "" {
		label = "box"
	}

	given := 0
	for _, v := range []string{opts.BoxName, opts.BoxID, opts.BoxIndexName} {
		if v != "" {
			given++
		}
	}
	if !opts.AllowNoArgs && given == 0 {
		return "", fmt.Errorf("No %s name, id or index name provided.", label)
	}
	if given > 1 {
		return "", errors.New("Cannot provide more than one of `box-name`, `box-full-name` or `box-id`.")
	}
	if opts.MatchMode != "" && opts.BoxName == "" {
		return "", errors.New("`box-name` must be provided if `name-match-mode` is provided.")
	}
	if opts.PickFirst && opts.BoxName == "" {
		return "", errors.New("`box-name` must be provided if `pick-first` is provided.")
	}
	if opts.MatchMode != "" && !opts.MatchMode.Valid() {
		return "", fmt.Errorf("invalid name match mode: %q", opts.MatchMode)
	}

	if opts.BoxIndexName != "" {
		return opts.BoxIndexName, nil
	}

	if opts.BoxID != "" {
		for _, bm := range metas {
			if bm.BoxID() == opts.BoxID {
				return bm.IndexName(), nil
			}
		}
		return "", fmt.Errorf("Box with id `%s` not found.", opts.BoxID)
	}

	// Either a name to match, or nothing at all — in which case every box is a
	// candidate and the picker decides.
	candidates := metas
	if opts.BoxName != "" {
		mode := opts.MatchMode
		if mode == "" {
			mode = MatchContains
		}
		candidates = filterByName(metas, opts.BoxName, mode, opts.MatchCase)
	}

	sorted := make([]*models.BoxMeta, len(candidates))
	copy(sorted, candidates)
	sort.Slice(sorted, func(i, j int) bool {
		return sorted[i].IndexName() < sorted[j].IndexName()
	})

	switch {
	case len(sorted) == 0:
		return "", ErrNotFound
	case len(sorted) == 1:
		return sorted[0].IndexName(), nil
	case opts.PickFirst:
		return sorted[0].IndexName(), nil
	}

	if p == nil {
		return "", fmt.Errorf("%d boxes match; narrow the selection or use --pick-first", len(sorted))
	}
	terms := make([]string, len(sorted))
	disp := make([]string, len(sorted))
	for i, bm := range sorted {
		terms[i] = bm.IndexName()
		groups := bm.Groups
		if groups == nil {
			groups = []string{}
		}
		disp[i] = fmt.Sprintf("%s (%s) groups: %s", bm.Name, bm.BoxID(), strings.Join(groups, ", "))
	}
	_, chosen, err := p.Pick(terms, disp)
	if err != nil {
		return "", err
	}
	return chosen, nil
}

func filterByName(metas []*models.BoxMeta, term string, mode MatchMode, matchCase bool) []*models.BoxMeta {
	if !matchCase {
		term = strings.ToLower(term)
	}
	var out []*models.BoxMeta
	for _, bm := range metas {
		name := bm.Name
		if !matchCase {
			name = strings.ToLower(name)
		}
		var hit bool
		switch mode {
		case MatchExact:
			hit = name == term
		case MatchContains:
			hit = strings.Contains(name, term)
		case MatchSubsequence:
			hit = IsSubsequenceMatch(term, name)
		}
		if hit {
			out = append(out, bm)
		}
	}
	return out
}

// IsSubsequenceMatch reports whether every character of term appears in name in
// order, though not necessarily adjacently.
//
// Compared over RUNES rather than bytes: Python indexes `term[j]` by character,
// so a multi-byte name would otherwise be compared a byte at a time and match
// differently.
//
// An EMPTY term matches everything, which is Python's behaviour and is relied
// on by the "no name given" path.
func IsSubsequenceMatch(term, name string) bool {
	t := []rune(term)
	if len(t) == 0 {
		return true
	}
	j := 0
	for _, ch := range name {
		if j < len(t) && ch == t[j] {
			j++
			if j == len(t) {
				return true
			}
		}
	}
	return j == len(t)
}

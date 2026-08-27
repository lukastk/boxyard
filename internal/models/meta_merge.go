package models

import (
	"fmt"
	"reflect"
	"sort"
	"strings"
)

// Merging a boxmeta that BOTH sides have edited, against the state they last
// agreed on.
//
// Today such a boxmeta is a dead end: sync sees two records that disagree,
// cannot tell which fields moved on which side, and refuses. Forty-four boxes
// on macbook stopped propagating their groups for a day in August 2026 that
// way, and each one needed a human.
//
// The merge reads each side's intent as a DELTA against the base rather than
// as a value to choose between, which is what makes it different from picking
// a winner: macbook's `archived` and mymain's `write_owner` both survive.

// MetaMergeConflict reports that both sides changed the same scalar to
// different values — the one case a human still has to settle.
type MetaMergeConflict struct {
	Fields []string
}

func (e *MetaMergeConflict) Error() string {
	return "cannot merge: both sides changed " + strings.Join(e.Fields, ", ") + " to different values"
}

// mergeSetField merges a field that behaves like a SET (`groups`, `parents`).
//
// A removal beats an addition. If one side deleted an entry the other still
// has, the deletion is the newer intent about that entry — the other side
// simply never saw it change. The reverse rule would make a group impossible
// to remove while any machine was behind.
//
// ORDER has to be a function of the content alone, never of which side happens
// to be "local": surviving base entries keep base order, and the additions
// follow SORTED. That sort is not tidiness. Ordering additions local-first put
// 80 of the 512 possible three-group merges in a different order on each
// machine — same set, different bytes — so each side read the other's push as
// a change and pushed back, trading the same boxmeta every 20 minutes forever.
func mergeSetField(base, local, remote []string) []string {
	inLocal, inRemote, inBase := toSet(local), toSet(remote), toSet(base)

	removed := map[string]bool{}
	added := map[string]bool{}
	for _, e := range base {
		if !inLocal[e] || !inRemote[e] {
			removed[e] = true
		}
	}
	for _, e := range append(append([]string{}, local...), remote...) {
		if !inBase[e] {
			added[e] = true
		}
	}

	out := []string{}
	seen := map[string]bool{}
	for _, e := range base {
		if !removed[e] && !seen[e] {
			out = append(out, e)
			seen[e] = true
		}
	}
	var additions []string
	for e := range added {
		if !removed[e] && !seen[e] {
			additions = append(additions, e)
		}
	}
	sort.Strings(additions)
	for _, e := range additions {
		if !seen[e] {
			out = append(out, e)
			seen[e] = true
		}
	}
	return out
}

func toSet(items []string) map[string]bool {
	out := make(map[string]bool, len(items))
	for _, i := range items {
		out[i] = true
	}
	return out
}

// mergeScalar merges a single value.
//
// One side changed it → take that side. Neither changed it → unchanged. BOTH
// changed it, differently → a real conflict, recorded and left for a human.
// That last case is the one thing a merge must not guess at: for `write_owner`
// it means two machines each believe they own the box, and picking one
// silently would hand it to a machine that does not think it has it.
func mergeScalar[T comparable](name string, base, local, remote T, conflicts *[]string) T {
	switch {
	case local == remote:
		return local
	case local == base:
		return remote
	case remote == base:
		return local
	default:
		*conflicts = append(*conflicts, name)
		return base
	}
}

// MergeBoxMetas merges a boxmeta both sides have edited.
//
// The merge is CONVERGENT rather than symmetric: machine A merges its local
// against the remote, pushes, and machine B then merges its own local against
// what A pushed. Each pass takes the union of the additions, so the fleet
// arrives at the same boxmeta without any machine seeing the others' bases.
func MergeBoxMetas(base, local, remote *BoxMeta) (*BoxMeta, error) {
	// The identity fields are not in the file — they come from the index name
	// and the storage location — so a disagreement here means the three
	// boxmetas are not describing the same box, which is a bug rather than an
	// edit to reconcile.
	for _, f := range []struct {
		name    string
		b, l, r string
	}{
		{"creation_timestamp_utc", base.CreationTimestampUTC, local.CreationTimestampUTC, remote.CreationTimestampUTC},
		{"box_subid", base.BoxSubid, local.BoxSubid, remote.BoxSubid},
		{"name", base.Name, local.Name, remote.Name},
	} {
		if f.b != f.l || f.l != f.r {
			return nil, fmt.Errorf("cannot merge boxmetas describing different boxes: %s is %q / %q / %q",
				f.name, f.b, f.l, f.r)
		}
	}

	var conflicts []string

	merged := *local
	merged.Groups = mergeSetField(base.Groups, local.Groups, remote.Groups)
	merged.Parents = mergeSetField(base.Parents, local.Parents, remote.Parents)
	merged.WriteOwner = mergeScalar("write_owner", base.WriteOwner, local.WriteOwner, remote.WriteOwner, &conflicts)
	merged.CreatorHostname = mergeScalar("creator_hostname",
		base.CreatorHostname, local.CreatorHostname, remote.CreatorHostname, &conflicts)

	// Keys from a NEWER boxyard merge per key. Dropping either side's would
	// silently discard a field this build does not understand, which is
	// exactly what UnknownKeys exists to prevent.
	mergedUnknown := map[string]any{}
	for key := range unionKeys(base.UnknownKeys, local.UnknownKeys, remote.UnknownKeys) {
		b, l, r := base.UnknownKeys[key], local.UnknownKeys[key], remote.UnknownKeys[key]
		value, ok := mergeAny("unknown_keys."+key, b, l, r, &conflicts)
		if ok {
			mergedUnknown[key] = value
		}
	}
	merged.UnknownKeys = mergedUnknown

	if len(conflicts) > 0 {
		sort.Strings(conflicts)
		return nil, &MetaMergeConflict{Fields: conflicts}
	}
	return &merged, nil
}

// mergeAny is mergeScalar for values that are not comparable with ==. It
// reports false when the merged value is absent, i.e. removed on one side.
func mergeAny(name string, base, local, remote any, conflicts *[]string) (any, bool) {
	switch {
	case reflect.DeepEqual(local, remote):
		return local, local != nil
	case reflect.DeepEqual(local, base):
		return remote, remote != nil
	case reflect.DeepEqual(remote, base):
		return local, local != nil
	default:
		*conflicts = append(*conflicts, name)
		return base, base != nil
	}
}

func unionKeys(maps ...map[string]any) map[string]bool {
	out := map[string]bool{}
	for _, m := range maps {
		for k := range m {
			out[k] = true
		}
	}
	return out
}

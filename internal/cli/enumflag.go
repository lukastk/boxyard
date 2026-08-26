package cli

import (
	"fmt"
	"strings"

	"github.com/spf13/pflag"
)

// enumFlag validates a flag's value at PARSE time.
//
// The Python declares these options with `Literal[...]` / an `Enum` subclass,
// so click rejects a bad value before the command body runs and exits 2. Go
// has no such annotation, and the port originally re-checked each value at the
// top of every RunE. That drifted in three ways at once:
//
//   - `boxyard rename --scope bogus` and `boxyard path --path-option bogus`
//     exited 1 where the Python exits 2, because their checks did not raise a
//     usage error (`path` also PRINTED "Invalid path option" for any failure
//     of the lookup, so an unrelated error would have been misreported as a
//     bad flag).
//   - `boxyard sync --sync-direction ”` exited 1: the empty string is the
//     port's "decide from the status" sentinel, but click has no sentinel — an
//     explicitly empty value is simply not one of push/pull.
//   - a bad value was invisible to the doctor-hint lint, which parses flags
//     but cannot run command bodies. The `--sync-direction to_remote` hint
//     that motivated that lint would have survived it.
//
// Validating in the flag itself fixes all three: one place per option, before
// the body runs, and reachable by anything that parses the command.
//
// The zero value of a flag is NOT run through Set, so an option like
// `--sync-direction` can still default to "" while rejecting an explicit "".
type enumFlag struct {
	target  *string
	allowed []string
}

func (e *enumFlag) String() string { return *e.target }

// Type is the metavar --help prints after the flag name. click renders an
// enum option as "[push|pull]", so this does too.
func (e *enumFlag) Type() string { return "[" + strings.Join(e.allowed, "|") + "]" }

func (e *enumFlag) Set(v string) error {
	for _, a := range e.allowed {
		if v == a {
			*e.target = v
			return nil
		}
	}
	return fmt.Errorf("must be one of %s", quotedList(e.allowed))
}

// enumSliceFlag is enumFlag for a repeatable option (`-c data -c meta`).
//
// It matches pflag's StringArray semantics, which is what these options used
// before: each occurrence appends, and there is no comma splitting (the Python
// takes `-c` more than once too, and a box part never contains a comma).
type enumSliceFlag struct {
	target  *[]string
	allowed []string
	// set records whether the flag has been seen, so the first occurrence
	// replaces the default rather than appending to it — pflag's own
	// stringArray does the same.
	set bool
}

// String returns "" for an empty selection so pflag treats it as a zero
// default and leaves "(default [])" out of --help, the way it does for its own
// string-array flags.
func (e *enumSliceFlag) String() string {
	if len(*e.target) == 0 {
		return ""
	}
	return "[" + strings.Join(*e.target, ",") + "]"
}

func (e *enumSliceFlag) Type() string { return "[" + strings.Join(e.allowed, "|") + "]" }

func (e *enumSliceFlag) Set(v string) error {
	for _, a := range e.allowed {
		if v == a {
			if !e.set {
				*e.target = nil
				e.set = true
			}
			*e.target = append(*e.target, v)
			return nil
		}
	}
	return fmt.Errorf("must be one of %s", quotedList(e.allowed))
}

func quotedList(vals []string) string {
	q := make([]string, len(vals))
	for i, v := range vals {
		q[i] = "'" + v + "'"
	}
	return strings.Join(q, ", ")
}

// enumVar registers an enum-valued option on fs.
func enumVar(fs *pflag.FlagSet, target *string, name, shorthand, def, usage string, allowed []string) {
	*target = def
	fs.VarP(&enumFlag{target: target, allowed: allowed}, name, shorthand, usage)
}

// enumSliceVar registers a repeatable enum-valued option on fs.
func enumSliceVar(fs *pflag.FlagSet, target *[]string, name, shorthand, usage string, allowed []string) {
	fs.VarP(&enumSliceFlag{target: target, allowed: allowed}, name, shorthand, usage)
}

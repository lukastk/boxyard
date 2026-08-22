// Package naming validates the identifiers boxyard interpolates into
// filesystem paths and remote paths.
//
// This lives in its own package because both config and models need it, and in
// Python it is a classmethod on BoxMeta that config reaches via a late import
// to dodge the circular dependency. Go has no late imports, so the shared rule
// gets a shared home.
package naming

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
)

// groupNameRe mirrors the Python r"^[A-Za-z0-9_\-/]+$". Group names become
// directory names in the group symlink tree, and "/" is meaningful there —
// "ctx/macbook" nests.
var groupNameRe = regexp.MustCompile(`^[A-Za-z0-9_\-/]+$`)

// ValidateGroupName reports whether a group name is usable.
func ValidateGroupName(name string) error {
	if !groupNameRe.MatchString(name) {
		return fmt.Errorf("invalid group name '%s'. Allowed characters: alphanumeric, '_', '-', '/'", name)
	}
	return nil
}

// ValidateBoxName reports whether a box name can be used as a box name.
//
// A box's index_name ({box_id}__{name}) is interpolated straight into
// filesystem paths, so a name that is not a single path component would spread
// the box over a nested directory tree — and the top level of that tree does
// not parse as a box registration, which breaks every subsequent
// create_boxyard_meta for the whole yard. Hence the strictness.
func ValidateBoxName(name string) error {
	if name == "" {
		return fmt.Errorf("Box name must be a non-empty string")
	}
	if name != strings.TrimSpace(name) {
		return fmt.Errorf("Box name must not have leading or trailing whitespace: %q", name)
	}
	if name == "." || name == ".." {
		return fmt.Errorf("Box name must not be %q", name)
	}
	if strings.HasPrefix(name, ".") {
		return fmt.Errorf("Box name must not start with a '.': %q", name)
	}
	for _, forbidden := range []string{"/", "\\", "\x00"} {
		if strings.Contains(name, forbidden) {
			return fmt.Errorf("Box name must be a single path component, but %q contains %q. Names are used verbatim as directory names", name, forbidden)
		}
	}
	if filepath.Base(name) != name {
		return fmt.Errorf("Box name must be a single path component: %q", name)
	}
	return nil
}

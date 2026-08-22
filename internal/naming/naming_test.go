package naming

import "testing"

func TestValidateGroupName(t *testing.T) {
	valid := []string{"proj", "ctx/macbook", "adu-me", "a_b", "all/proj", "a1/b2-c_3", "A"}
	for _, n := range valid {
		if err := ValidateGroupName(n); err != nil {
			t.Errorf("valid group name %q rejected: %v", n, err)
		}
	}
	invalid := []string{"", "bad name", "a.b", "a:b", "a\\b", "a\tb", "émoji", "a b/c"}
	for _, n := range invalid {
		if err := ValidateGroupName(n); err == nil {
			t.Errorf("invalid group name %q accepted", n)
		}
	}
}

// Ported from src/tests/unit/models/test_box_name_validation.py. A box name is
// interpolated verbatim into filesystem paths, so anything that is not a single
// path component corrupts the whole yard's registration scan.
func TestValidateBoxName(t *testing.T) {
	valid := []string{"my-project", "a", "boxyard-go", "a b", "name.with.dots", "UPPER", "123", "a--b__c"}
	for _, n := range valid {
		if err := ValidateBoxName(n); err != nil {
			t.Errorf("valid box name %q rejected: %v", n, err)
		}
	}
	invalid := map[string]string{
		"":            "empty",
		" leading":    "leading whitespace",
		"trailing ":   "trailing whitespace",
		".":           "dot",
		"..":          "dotdot",
		".hidden":     "leading dot",
		"a/b":         "slash",
		"a\\b":        "backslash",
		"a\x00b":      "null byte",
		"nested/path": "path separator",
		"/abs":        "absolute",
		"trail\n":     "trailing newline is whitespace",
	}
	for n, why := range invalid {
		if err := ValidateBoxName(n); err == nil {
			t.Errorf("invalid box name %q (%s) accepted", n, why)
		}
	}
}

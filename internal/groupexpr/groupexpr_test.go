package groupexpr

import (
	"strings"
	"testing"
)

// mustParse compiles expr, failing the test if it does not compile.
func mustParse(t *testing.T, expr string) func([]string) bool {
	t.Helper()
	pred, err := Parse(expr)
	if err != nil {
		t.Fatalf("Parse(%q) returned unexpected error: %v", expr, err)
	}
	if pred == nil {
		t.Fatalf("Parse(%q) returned a nil predicate with a nil error", expr)
	}
	return pred
}

// evalCase is one (expression, groups) -> want row.
type evalCase struct {
	name   string
	expr   string
	groups []string
	want   bool
}

// runEvalCases compiles each case's expression and checks the predicate.
func runEvalCases(t *testing.T, cases []evalCase) {
	t.Helper()
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pred := mustParse(t, tc.expr)
			if got := pred(tc.groups); got != tc.want {
				t.Errorf("Parse(%q)(%v) = %v, want %v", tc.expr, tc.groups, got, tc.want)
			}
		})
	}
}

// --- single group ---------------------------------------------------------

func TestSingleGroup(t *testing.T) {
	runEvalCases(t, []evalCase{
		{"match", "backend", []string{"backend", "api"}, true},
		{"no_match", "backend", []string{"api", "frontend"}, false},
		{"empty_groups", "backend", []string{}, false},
		// Go has no set input (the Python accepted set or list); duplicates in
		// the slice are the closest analogue and must not change the answer.
		{"duplicate_entries", "backend", []string{"backend", "backend", "api"}, true},
		{"nil_groups", "backend", nil, false},
	})
}

// --- AND ------------------------------------------------------------------

func TestAnd(t *testing.T) {
	runEvalCases(t, []evalCase{
		{"both_present", "backend AND api", []string{"backend", "api", "v2"}, true},
		{"left_missing", "backend AND api", []string{"api", "frontend"}, false},
		{"right_missing", "backend AND api", []string{"backend", "frontend"}, false},
		{"both_missing", "backend AND api", []string{"frontend", "legacy"}, false},
		{"chained_all", "a AND b AND c", []string{"a", "b", "c"}, true},
		{"chained_missing_c", "a AND b AND c", []string{"a", "b"}, false},
		{"chained_missing_b", "a AND b AND c", []string{"a", "c"}, false},
	})
}

// --- OR -------------------------------------------------------------------

func TestOr(t *testing.T) {
	runEvalCases(t, []evalCase{
		{"both_present", "backend OR api", []string{"backend", "api"}, true},
		{"left_only", "backend OR api", []string{"backend", "frontend"}, true},
		{"right_only", "backend OR api", []string{"api", "frontend"}, true},
		{"neither_present", "backend OR api", []string{"frontend", "legacy"}, false},
		{"chained_a", "a OR b OR c", []string{"a"}, true},
		{"chained_b", "a OR b OR c", []string{"b"}, true},
		{"chained_c", "a OR b OR c", []string{"c"}, true},
		{"chained_none", "a OR b OR c", []string{"d"}, false},
	})
}

// --- NOT ------------------------------------------------------------------

func TestNot(t *testing.T) {
	runEvalCases(t, []evalCase{
		{"group_present", "NOT deprecated", []string{"deprecated", "backend"}, false},
		{"group_absent", "NOT deprecated", []string{"backend", "api"}, true},
		{"empty_groups", "NOT deprecated", []string{}, true},
		{"double_not_present", "NOT NOT backend", []string{"backend"}, true},
		{"double_not_absent", "NOT NOT backend", []string{"api"}, false},
		{"triple_not_present", "NOT NOT NOT backend", []string{"backend"}, false},
	})
}

// --- precedence -----------------------------------------------------------

func TestPrecedence(t *testing.T) {
	runEvalCases(t, []evalCase{
		// AND binds tighter than OR: "a OR b AND c" == "a OR (b AND c)".
		{"and_over_or_a", "a OR b AND c", []string{"a"}, true},
		{"and_over_or_bc", "a OR b AND c", []string{"b", "c"}, true},
		{"and_over_or_b", "a OR b AND c", []string{"b"}, false},

		// NOT binds tighter than AND: "NOT a AND b" == "(NOT a) AND b".
		{"not_over_and_b", "NOT a AND b", []string{"b"}, true},
		{"not_over_and_ab", "NOT a AND b", []string{"a", "b"}, false},
		{"not_over_and_a", "NOT a AND b", []string{"a"}, false},

		// NOT binds tighter than OR: "NOT a OR b" == "(NOT a) OR b".
		{"not_over_or_none", "NOT a OR b", []string{}, true},
		{"not_over_or_a", "NOT a OR b", []string{"a"}, false},
		{"not_over_or_ab", "NOT a OR b", []string{"a", "b"}, true},

		// "NOT a OR b AND c" == "(NOT a) OR (b AND c)".
		{"complex_none", "NOT a OR b AND c", []string{}, true},
		{"complex_a", "NOT a OR b AND c", []string{"a"}, false},
		{"complex_bc", "NOT a OR b AND c", []string{"b", "c"}, true},
		{"complex_abc", "NOT a OR b AND c", []string{"a", "b", "c"}, true},

		// OR is left-associative; the shape is unobservable for pure OR but
		// pinning it guards against an accidental right fold.
		{"or_left_assoc", "a OR b OR c", []string{"b"}, true},
	})
}

// --- parentheses ----------------------------------------------------------

func TestParentheses(t *testing.T) {
	runEvalCases(t, []evalCase{
		{"override_ac", "(a OR b) AND c", []string{"a", "c"}, true},
		{"override_a", "(a OR b) AND c", []string{"a"}, false},
		{"override_bc", "(a OR b) AND c", []string{"b", "c"}, true},

		{"not_parens_ab", "NOT (a AND b)", []string{"a", "b"}, false},
		{"not_parens_a", "NOT (a AND b)", []string{"a"}, true},
		{"not_parens_none", "NOT (a AND b)", []string{}, true},

		{"nested_ac", "((a OR b) AND c)", []string{"a", "c"}, true},
		{"nested_bc", "((a OR b) AND c)", []string{"b", "c"}, true},
		{"nested_c", "((a OR b) AND c)", []string{"c"}, false},

		{"deep_ab", "((a AND (b OR c)) OR (d AND e))", []string{"a", "b"}, true},
		{"deep_ac", "((a AND (b OR c)) OR (d AND e))", []string{"a", "c"}, true},
		{"deep_de", "((a AND (b OR c)) OR (d AND e))", []string{"d", "e"}, true},
		{"deep_a", "((a AND (b OR c)) OR (d AND e))", []string{"a"}, false},
		{"deep_d", "((a AND (b OR c)) OR (d AND e))", []string{"d"}, false},

		{"redundant_parens", "(((a)))", []string{"a"}, true},
	})
}

func TestComplexExpressionWithAllOperators(t *testing.T) {
	const expr = "(backend OR frontend) AND NOT (deprecated OR legacy)"
	runEvalCases(t, []evalCase{
		{"backend", expr, []string{"backend"}, true},
		{"frontend", expr, []string{"frontend"}, true},
		{"backend_api", expr, []string{"backend", "api"}, true},
		{"api_only", expr, []string{"api"}, false},
		{"backend_deprecated", expr, []string{"backend", "deprecated"}, false},
		{"frontend_legacy", expr, []string{"frontend", "legacy"}, false},
	})
}

// --- whitespace -----------------------------------------------------------

func TestWhitespaceHandling(t *testing.T) {
	runEvalCases(t, []evalCase{
		{"extra_spaces_true", "a  AND  b", []string{"a", "b"}, true},
		{"extra_spaces_false", "a  AND  b", []string{"a"}, false},
		{"leading_trailing_spaces", "  a AND b  ", []string{"a", "b"}, true},
		{"spaces_around_parens", "( a OR b ) AND c", []string{"a", "c"}, true},
		{"no_spaces_around_parens", "(a OR b)AND c", []string{"a", "c"}, true},
		{"no_space_after_not", "NOT(a)", []string{"a"}, false},
		{"no_space_after_not_absent", "NOT(a)", []string{"b"}, true},
		{"no_space_before_lparen", "a AND(b)", []string{"a", "b"}, true},
		{"tabs_and_newlines", "a\tAND\nb", []string{"a", "b"}, true},
		{"crlf", "a AND\r\nb", []string{"a", "b"}, true},
		{"leading_newline", "\n a AND b \n", []string{"a", "b"}, true},
	})
}

// --- operator casing ------------------------------------------------------

func TestOperatorCasing(t *testing.T) {
	runEvalCases(t, []evalCase{
		{"lowercase_and", "a and b", []string{"a", "b"}, true},
		{"lowercase_or", "a or b", []string{"a"}, true},
		{"lowercase_not", "not a", []string{"b"}, true},
		{"lowercase_not_present", "not a", []string{"a"}, false},
		{"mixed_case_ab", "a And b Or c", []string{"a", "b"}, true},
		{"mixed_case_c", "a And b Or c", []string{"c"}, true},
		{"mixed_case_none", "a And b Or c", []string{"a"}, false},
		{"weird_casing", "aNd_x aNd b", []string{"aNd_x", "b"}, true},
		{"uppercase_not_lowercase_and", "(NOT a) and (not b)", []string{"c"}, true},
		{"uppercase_not_lowercase_and_hit", "(NOT a) and (not b)", []string{"b"}, false},
	})
}

// --- group names ----------------------------------------------------------

func TestGroupNames(t *testing.T) {
	runEvalCases(t, []evalCase{
		{"alphanumeric", "backend123", []string{"backend123"}, true},
		{"hyphenated", "my-group AND other-group", []string{"my-group", "other-group"}, true},
		{"underscored", "my_group AND other_group", []string{"my_group", "other_group"}, true},
		{"slashed", "category/subcategory", []string{"category/subcategory"}, true},
		{"complex", "my-project_v2/prod AND api-v3", []string{"my-project_v2/prod", "api-v3"}, true},
		{"numeric", "2024 AND v2", []string{"2024", "v2"}, true},
		{"single_char", "a AND b", []string{"a", "b"}, true},
		// Unicode letters are identifier characters (Python's str.isalnum()).
		{"unicode_letters", "café AND naïve", []string{"café", "naïve"}, true},
		{"unicode_letters_miss", "café AND naïve", []string{"cafe", "naïve"}, false},
	})
}

func TestGroupNamesAreCaseSensitive(t *testing.T) {
	runEvalCases(t, []evalCase{
		{"exact", "Backend", []string{"Backend"}, true},
		{"lower", "Backend", []string{"backend"}, false},
		{"upper", "Backend", []string{"BACKEND"}, false},
	})
}

// TestOperatorWordBoundary pins the tokenizer rule that an operator only
// matches when it is not glued to further identifier characters.
func TestOperatorWordBoundary(t *testing.T) {
	runEvalCases(t, []evalCase{
		{"android_both", "android AND api", []string{"android", "api"}, true},
		{"android_left", "android AND api", []string{"android"}, false},
		{"android_right", "android AND api", []string{"api"}, false},

		{"oracle_both", "oracle AND api", []string{"oracle", "api"}, true},
		{"oracle_left", "oracle AND api", []string{"oracle"}, false},
		{"oracle_right", "oracle AND api", []string{"api"}, false},

		{"notebook_both", "notebook AND api", []string{"notebook", "api"}, true},
		{"notebook_left", "notebook AND api", []string{"notebook"}, false},
		{"notebook_right", "notebook AND api", []string{"api"}, false},

		{"android_upper", "ANDroid", []string{"ANDroid"}, true},
		{"or_prefix_digit", "or2", []string{"or2"}, true},
		// '-' and '/' are identifier characters, so these are single names,
		// NOT a negation of "a".
		{"not_hyphen_is_identifier", "not-a", []string{"not-a"}, true},
		{"not_hyphen_not_negation", "not-a", []string{"a"}, false},
		{"not_slash_is_identifier", "not/a", []string{"not/a"}, true},
		{"and_underscore_is_identifier", "and_b", []string{"and_b"}, true},
		// A keyword followed by a non-identifier char is still a keyword.
		{"not_before_paren", "NOT(a)", []string{}, true},
	})
}

// --- error cases ----------------------------------------------------------

func TestParseErrors(t *testing.T) {
	cases := []struct {
		name    string
		expr    string
		wantSub string
	}{
		{"empty", "", "empty expression"},
		{"whitespace_only", "   ", "empty expression"},
		{"newlines_only", "\n\t \n", "empty expression"},

		{"unmatched_open_paren", "(a AND b", "unmatched opening parenthesis"},
		{"unmatched_open_paren_nested", "((a)", "unmatched opening parenthesis"},
		{"lone_open_paren", "(", "unexpected end of expression"},
		{"unmatched_close_paren", "a AND b)", "unexpected token at position 3: )"},
		{"lone_close_paren", ")", "unexpected operator or parenthesis: )"},

		{"trailing_and", "a AND", "unexpected end of expression"},
		{"trailing_or", "a OR", "unexpected end of expression"},
		{"trailing_not", "a AND NOT", "unexpected end of expression"},
		{"lone_not", "NOT", "unexpected end of expression"},

		{"leading_and", "AND a", "unexpected operator or parenthesis: AND"},
		{"leading_or", "OR a", "unexpected operator or parenthesis: OR"},
		{"lone_and", "AND", "unexpected operator or parenthesis: AND"},

		{"double_and", "a AND AND b", "unexpected operator or parenthesis: AND"},
		{"double_or", "a OR OR b", "unexpected operator or parenthesis: OR"},
		{"and_then_or", "a AND OR b", "unexpected operator or parenthesis: OR"},

		{"invalid_char_ampersand", "a & b", "invalid character at position 2: &"},
		{"invalid_char_pipe", "a | b", "invalid character at position 2: |"},
		{"invalid_char_bang", "!a", "invalid character at position 0: !"},
		{"invalid_char_quote", `"a"`, `invalid character at position 0: "`},
		{"invalid_char_comma", "a, b", "invalid character at position 1: ,"},
		{"invalid_char_dot", "a.b", "invalid character at position 1: ."},
		// The reported position is a rune offset into the TRIMMED expression.
		{"invalid_char_position_after_trim", "   a & b", "invalid character at position 2: &"},
		{"invalid_char_position_unicode", "café & b", "invalid character at position 5: &"},

		{"empty_parens", "a AND ()", "unexpected operator or parenthesis: )"},
		{"empty_parens_alone", "()", "unexpected operator or parenthesis: )"},
		{"or_before_close_paren", "a AND (b OR)", "unexpected operator or parenthesis: )"},

		// Two adjacent atoms: the first is parsed, the rest is unconsumed.
		{"adjacent_identifiers", "a b", "unexpected token at position 1: b"},
		{"identifier_glued_to_and", "aAND b", "unexpected token at position 1: b"},
		// "AND-b" is one identifier because '-' is an identifier character.
		{"and_hyphen_identifier", "a AND-b", "unexpected token at position 1: AND-b"},
		{"trailing_close_paren_after_group", "(a) )", "unexpected token at position 3: )"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			pred, err := Parse(tc.expr)
			if err == nil {
				t.Fatalf("Parse(%q) succeeded, want error containing %q", tc.expr, tc.wantSub)
			}
			if pred != nil {
				t.Errorf("Parse(%q) returned a non-nil predicate alongside error %v", tc.expr, err)
			}
			if !strings.Contains(err.Error(), tc.wantSub) {
				t.Errorf("Parse(%q) error = %q, want it to contain %q", tc.expr, err, tc.wantSub)
			}
		})
	}
}

// TestNestingDepthLimit checks that pathological nesting produces a loud error
// rather than a stack overflow.
func TestNestingDepthLimit(t *testing.T) {
	for _, tc := range []struct {
		name string
		expr string
	}{
		{"nots", strings.Repeat("NOT ", maxDepth+10) + "a"},
		{"parens", strings.Repeat("(", maxDepth+10) + "a" + strings.Repeat(")", maxDepth+10)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Parse(tc.expr); err == nil {
				t.Fatal("Parse succeeded, want a nesting-depth error")
			} else if !strings.Contains(err.Error(), "nested too deeply") {
				t.Fatalf("Parse error = %q, want a nesting-depth error", err)
			}
		})
	}

	// Just under the limit must still compile and evaluate.
	expr := strings.Repeat("NOT ", 100) + "a" // even count -> identity
	pred := mustParse(t, expr)
	if !pred([]string{"a"}) {
		t.Error("100 NOTs should cancel out to identity")
	}
}

// --- predicate behaviour --------------------------------------------------

func TestPredicateIsReusable(t *testing.T) {
	pred := mustParse(t, "a AND b")
	cases := []struct {
		groups []string
		want   bool
	}{
		{[]string{"a", "b"}, true},
		{[]string{"a"}, false},
		{[]string{"a", "b", "c"}, true},
		{[]string{}, false},
		{[]string{"a", "b"}, true},
	}
	for _, tc := range cases {
		if got := pred(tc.groups); got != tc.want {
			t.Errorf("pred(%v) = %v, want %v", tc.groups, got, tc.want)
		}
	}
}

func TestPredicateDoesNotModifyInput(t *testing.T) {
	groups := []string{"a", "b"}
	original := append([]string(nil), groups...)
	pred := mustParse(t, "a AND b")
	pred(groups)
	if len(groups) != len(original) {
		t.Fatalf("predicate changed the slice length: %v -> %v", original, groups)
	}
	for i := range groups {
		if groups[i] != original[i] {
			t.Fatalf("predicate mutated the input slice: %v -> %v", original, groups)
		}
	}
}

func TestGroupsNotInExpressionAreIgnored(t *testing.T) {
	pred := mustParse(t, "a AND b")
	if !pred([]string{"a", "b", "c", "d", "e"}) {
		t.Error("extra groups should not affect the result")
	}
}

func TestPredicateIsSafeForConcurrentUse(t *testing.T) {
	pred := mustParse(t, "(a OR b) AND NOT c")
	done := make(chan bool, 8)
	for i := 0; i < 8; i++ {
		go func() {
			ok := true
			for n := 0; n < 200; n++ {
				ok = ok && pred([]string{"a"}) && !pred([]string{"a", "c"}) && !pred([]string{"d"})
			}
			done <- ok
		}()
	}
	for i := 0; i < 8; i++ {
		if !<-done {
			t.Fatal("concurrent evaluation produced a wrong result")
		}
	}
}

// --- realistic scenarios --------------------------------------------------

func TestScenarios(t *testing.T) {
	const (
		notDeprecated = "backend AND NOT deprecated"
		environments  = "(prod OR staging) AND NOT legacy"
		hierarchical  = "company/team-a OR company/team-b"
		project       = "(backend OR frontend) AND (prod OR staging) AND NOT (deprecated OR archived)"
	)
	runEvalCases(t, []evalCase{
		{"backend_not_deprecated_ok", notDeprecated, []string{"backend", "api"}, true},
		{"backend_not_deprecated_hit", notDeprecated, []string{"backend", "deprecated"}, false},
		{"backend_not_deprecated_miss", notDeprecated, []string{"frontend"}, false},

		{"environments_prod", environments, []string{"prod", "backend"}, true},
		{"environments_staging", environments, []string{"staging", "frontend"}, true},
		{"environments_legacy", environments, []string{"prod", "legacy"}, false},
		{"environments_dev", environments, []string{"dev"}, false},

		{"hierarchical_a", hierarchical, []string{"company/team-a"}, true},
		{"hierarchical_b", hierarchical, []string{"company/team-b"}, true},
		{"hierarchical_c", hierarchical, []string{"company/team-c"}, false},

		{"project_backend_prod", project, []string{"backend", "prod"}, true},
		{"project_frontend_staging", project, []string{"frontend", "staging"}, true},
		{"project_multi", project, []string{"backend", "frontend", "prod"}, true},
		{"project_no_env", project, []string{"backend"}, false},
		{"project_deprecated", project, []string{"backend", "prod", "deprecated"}, false},
		{"project_archived", project, []string{"frontend", "staging", "archived"}, false},
	})
}

// liveConfigExprs are the filter_expr values from a real
// ~/.config/boxyard/config.toml [virtual_box_groups.*] section, including the
// multi-line ones that mix "NOT" and "not". Every one of these must compile.
var liveConfigExprs = []string{
	"(NOT archived) AND (NOT null)",
	"archived",
	"(NOT archived) AND (NOT null) AND proj",
	"archived AND proj",
	"(NOT archived) AND (NOT null) AND mysetup",
	"archived AND mysetup",
	"(NOT archived) AND (NOT null) AND adu-me",
	"archived AND adu-me",
	"(NOT archived) AND (NOT null) AND adu-team",
	"archived AND adu-team",
	"(NOT archived) AND (NOT null) AND physics",
	"archived AND physics",
	"(NOT archived) AND (NOT null) AND scuttlebug",
	"archived AND scuttlebug",
	"(NOT archived) AND (NOT null) AND templates",
	"archived AND templates",
	"(NOT archived) AND (NOT null) AND worktrees",
	"archived AND worktrees",
	"(NOT archived) AND (NOT null) AND politick",
	"archived AND politick",
	"(NOT archived) AND (NOT null) AND corkboard",
	"archived AND corkboard",
	"(NOT archived) AND (NOT null) AND mytutor",
	"archived AND mytutor",
	// The "other" buckets: multi-line, lowercase "not", and one line with a
	// trailing space before the newline.
	liveConfigExprsOtherActive,
	liveConfigExprsOtherArchived,
}

func TestLiveConfigExpressionsCompile(t *testing.T) {
	for _, expr := range liveConfigExprs {
		if _, err := Parse(expr); err != nil {
			t.Errorf("Parse(%q) failed: %v", expr, err)
		}
	}
}

func TestLiveConfigExpressionsEvaluate(t *testing.T) {
	const (
		otherActive   = liveConfigExprsOtherActive
		otherArchived = liveConfigExprsOtherArchived
	)
	runEvalCases(t, []evalCase{
		{"active_plain", "(NOT archived) AND (NOT null)", []string{"proj"}, true},
		{"active_is_archived", "(NOT archived) AND (NOT null)", []string{"archived", "proj"}, false},
		{"active_is_null", "(NOT archived) AND (NOT null)", []string{"null"}, false},
		{"active_no_groups", "(NOT archived) AND (NOT null)", []string{}, true},

		{"archived_hit", "archived", []string{"archived", "proj"}, true},
		{"archived_miss", "archived", []string{"proj"}, false},

		{"active_proj_hit", "(NOT archived) AND (NOT null) AND proj", []string{"proj"}, true},
		{"active_proj_archived", "(NOT archived) AND (NOT null) AND proj", []string{"proj", "archived"}, false},
		{"active_proj_wrong_cat", "(NOT archived) AND (NOT null) AND proj", []string{"mysetup"}, false},

		{"archived_proj_hit", "archived AND proj", []string{"archived", "proj"}, true},
		{"archived_proj_miss", "archived AND proj", []string{"archived", "physics"}, false},

		{"hyphenated_category", "archived AND adu-me", []string{"archived", "adu-me"}, true},
		{"hyphenated_category_miss", "archived AND adu-me", []string{"archived", "adu-team"}, false},

		// The multi-line "other" bucket: an uncategorised, unarchived box.
		{"other_uncategorised", otherActive, []string{"scratch"}, true},
		{"other_no_groups", otherActive, []string{}, true},
		{"other_has_category", otherActive, []string{"proj"}, false},
		{"other_has_late_category", otherActive, []string{"mytutor"}, false},
		{"other_archived", otherActive, []string{"archived"}, false},
		{"other_null", otherActive, []string{"null"}, false},

		{"other_archived_hit", otherArchived, []string{"archived"}, true},
		{"other_archived_categorised", otherArchived, []string{"archived", "physics"}, false},
		{"other_archived_not_archived", otherArchived, []string{"scratch"}, false},
	})
}

// The two multi-line buckets, spelled out for the evaluation table above.
const (
	liveConfigExprsOtherActive = "(NOT archived) AND\n(NOT null) AND\n(not proj) AND\n" +
		"(not mysetup) AND\n(not adu-me) AND\n(not adu-team) AND\n(not physics) AND\n" +
		"(not scuttlebug) AND \n(not templates) AND\n(not worktrees) AND\n" +
		"(not politick) AND\n(not corkboard) AND\n(not mytutor)\n"

	liveConfigExprsOtherArchived = "archived AND\n(not proj) AND\n(not mysetup) AND\n" +
		"(not adu-me) AND\n(not adu-team) AND\n(not physics) AND\n(not scuttlebug) AND\n" +
		"(not templates) AND\n(not worktrees) AND\n(not politick) AND\n" +
		"(not corkboard) AND\n(not mytutor)\n"
)

// --- tokenizer character classes ------------------------------------------

// TestCharacterClasses pins the identifier/whitespace classification, which is
// a direct port of Python's str.isalnum() / str.isspace().
func TestCharacterClasses(t *testing.T) {
	identifierChars := []rune{'a', 'Z', '0', '9', '_', '-', '/', 'é', 'Ω', '中', '٣'}
	for _, r := range identifierChars {
		if !isIdentifierChar(r) {
			t.Errorf("isIdentifierChar(%q) = false, want true", r)
		}
	}
	nonIdentifierChars := []rune{' ', '(', ')', '&', '|', '!', '.', ',', '"', '\'', '\\', '+', '*', '#', '@'}
	for _, r := range nonIdentifierChars {
		if isIdentifierChar(r) {
			t.Errorf("isIdentifierChar(%q) = true, want false", r)
		}
	}

	// Python's str.isspace() also covers the four information separators, so
	// the port does too.
	spaceChars := []rune{' ', '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0xa0, 0x2028, 0x3000}
	for _, r := range spaceChars {
		if !isSpace(r) {
			t.Errorf("isSpace(%U) = false, want true", r)
		}
	}
	for _, r := range []rune{'a', '_', 0x200b} {
		if isSpace(r) {
			t.Errorf("isSpace(%U) = true, want false", r)
		}
	}
}

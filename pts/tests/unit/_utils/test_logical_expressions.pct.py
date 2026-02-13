# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Tests for _utils.logical_expressions
#
# This module tests the boolean expression parser used for virtual group filtering.
# The parser supports AND, OR, NOT operators with precedence: NOT > AND > OR

# %%
#|default_exp unit._utils.test_logical_expressions

# %%
#|export
import pytest
from boxyard._utils.logical_expressions import get_group_filter_func

# %% [markdown]
# ## 1. Basic Operators - Single Group

# %%
#|export
def test_single_group_match():
    """Single group name matches when present in groups."""
    filter_func = get_group_filter_func("backend")
    assert filter_func(["backend", "api"]) == True

# %%
#|export
def test_single_group_no_match():
    """Single group name doesn't match when absent from groups."""
    filter_func = get_group_filter_func("backend")
    assert filter_func(["api", "frontend"]) == False

# %%
#|export
def test_single_group_with_set_input():
    """Filter function accepts set input."""
    filter_func = get_group_filter_func("backend")
    assert filter_func({"backend", "api"}) == True

# %%
#|export
def test_single_group_empty_groups():
    """Single group doesn't match empty group list."""
    filter_func = get_group_filter_func("backend")
    assert filter_func([]) == False

# %% [markdown]
# ## 2. AND Operator

# %%
#|export
def test_and_both_present():
    """AND returns True when both groups are present."""
    filter_func = get_group_filter_func("backend AND api")
    assert filter_func(["backend", "api", "v2"]) == True

# %%
#|export
def test_and_left_missing():
    """AND returns False when left group is missing."""
    filter_func = get_group_filter_func("backend AND api")
    assert filter_func(["api", "frontend"]) == False

# %%
#|export
def test_and_right_missing():
    """AND returns False when right group is missing."""
    filter_func = get_group_filter_func("backend AND api")
    assert filter_func(["backend", "frontend"]) == False

# %%
#|export
def test_and_both_missing():
    """AND returns False when both groups are missing."""
    filter_func = get_group_filter_func("backend AND api")
    assert filter_func(["frontend", "legacy"]) == False

# %%
#|export
def test_and_chained():
    """Chained AND operators work correctly."""
    filter_func = get_group_filter_func("a AND b AND c")
    assert filter_func(["a", "b", "c"]) == True
    assert filter_func(["a", "b"]) == False
    assert filter_func(["a", "c"]) == False

# %% [markdown]
# ## 3. OR Operator

# %%
#|export
def test_or_both_present():
    """OR returns True when both groups are present."""
    filter_func = get_group_filter_func("backend OR api")
    assert filter_func(["backend", "api"]) == True

# %%
#|export
def test_or_left_only():
    """OR returns True when only left group is present."""
    filter_func = get_group_filter_func("backend OR api")
    assert filter_func(["backend", "frontend"]) == True

# %%
#|export
def test_or_right_only():
    """OR returns True when only right group is present."""
    filter_func = get_group_filter_func("backend OR api")
    assert filter_func(["api", "frontend"]) == True

# %%
#|export
def test_or_neither_present():
    """OR returns False when neither group is present."""
    filter_func = get_group_filter_func("backend OR api")
    assert filter_func(["frontend", "legacy"]) == False

# %%
#|export
def test_or_chained():
    """Chained OR operators work correctly."""
    filter_func = get_group_filter_func("a OR b OR c")
    assert filter_func(["a"]) == True
    assert filter_func(["b"]) == True
    assert filter_func(["c"]) == True
    assert filter_func(["d"]) == False

# %% [markdown]
# ## 4. NOT Operator

# %%
#|export
def test_not_group_present():
    """NOT returns False when group is present."""
    filter_func = get_group_filter_func("NOT deprecated")
    assert filter_func(["deprecated", "backend"]) == False

# %%
#|export
def test_not_group_absent():
    """NOT returns True when group is absent."""
    filter_func = get_group_filter_func("NOT deprecated")
    assert filter_func(["backend", "api"]) == True

# %%
#|export
def test_not_empty_groups():
    """NOT returns True for empty group list."""
    filter_func = get_group_filter_func("NOT deprecated")
    assert filter_func([]) == True

# %%
#|export
def test_double_not():
    """Double NOT cancels out."""
    filter_func = get_group_filter_func("NOT NOT backend")
    assert filter_func(["backend"]) == True
    assert filter_func(["api"]) == False

# %% [markdown]
# ## 5. Operator Precedence
#
# Precedence: NOT > AND > OR

# %%
#|export
def test_precedence_and_binds_tighter_than_or():
    """AND binds tighter than OR: 'a OR b AND c' == 'a OR (b AND c)'"""
    filter_func = get_group_filter_func("a OR b AND c")
    # Should be equivalent to: a OR (b AND c)
    # With groups [a]: a=T, b=F, c=F -> T OR (F AND F) -> T OR F -> T
    assert filter_func(["a"]) == True
    # With groups [b, c]: a=F, b=T, c=T -> F OR (T AND T) -> F OR T -> T
    assert filter_func(["b", "c"]) == True
    # With groups [b]: a=F, b=T, c=F -> F OR (T AND F) -> F OR F -> F
    assert filter_func(["b"]) == False

# %%
#|export
def test_precedence_not_binds_tighter_than_and():
    """NOT binds tighter than AND: 'NOT a AND b' == '(NOT a) AND b'"""
    filter_func = get_group_filter_func("NOT a AND b")
    # Should be equivalent to: (NOT a) AND b
    # With groups [b]: a=F, b=T -> (NOT F) AND T -> T AND T -> T
    assert filter_func(["b"]) == True
    # With groups [a, b]: a=T, b=T -> (NOT T) AND T -> F AND T -> F
    assert filter_func(["a", "b"]) == False
    # With groups [a]: a=T, b=F -> (NOT T) AND F -> F AND F -> F
    assert filter_func(["a"]) == False

# %%
#|export
def test_precedence_not_binds_tighter_than_or():
    """NOT binds tighter than OR: 'NOT a OR b' == '(NOT a) OR b'"""
    filter_func = get_group_filter_func("NOT a OR b")
    # Should be equivalent to: (NOT a) OR b
    # With groups []: a=F, b=F -> (NOT F) OR F -> T OR F -> T
    assert filter_func([]) == True
    # With groups [a]: a=T, b=F -> (NOT T) OR F -> F OR F -> F
    assert filter_func(["a"]) == False
    # With groups [a, b]: a=T, b=T -> (NOT T) OR T -> F OR T -> T
    assert filter_func(["a", "b"]) == True

# %%
#|export
def test_precedence_complex():
    """Complex expression: 'NOT a OR b AND c' == '(NOT a) OR (b AND c)'"""
    filter_func = get_group_filter_func("NOT a OR b AND c")
    # Should be equivalent to: (NOT a) OR (b AND c)
    # With groups []: -> (NOT F) OR (F AND F) -> T OR F -> T
    assert filter_func([]) == True
    # With groups [a]: -> (NOT T) OR (F AND F) -> F OR F -> F
    assert filter_func(["a"]) == False
    # With groups [b, c]: -> (NOT F) OR (T AND T) -> T OR T -> T
    assert filter_func(["b", "c"]) == True
    # With groups [a, b, c]: -> (NOT T) OR (T AND T) -> F OR T -> T
    assert filter_func(["a", "b", "c"]) == True

# %% [markdown]
# ## 6. Parentheses

# %%
#|export
def test_parens_override_precedence():
    """Parentheses override default precedence."""
    filter_func = get_group_filter_func("(a OR b) AND c")
    # Without parens: a OR (b AND c)
    # With parens: (a OR b) AND c
    # Groups [a, c]: (T OR F) AND T -> T AND T -> T
    assert filter_func(["a", "c"]) == True
    # Groups [a]: (T OR F) AND F -> T AND F -> F
    assert filter_func(["a"]) == False
    # Groups [b, c]: (F OR T) AND T -> T AND T -> T
    assert filter_func(["b", "c"]) == True

# %%
#|export
def test_parens_with_not():
    """NOT with parentheses: 'NOT (a AND b)'"""
    filter_func = get_group_filter_func("NOT (a AND b)")
    # NOT (a AND b)
    # Groups [a, b]: NOT (T AND T) -> NOT T -> F
    assert filter_func(["a", "b"]) == False
    # Groups [a]: NOT (T AND F) -> NOT F -> T
    assert filter_func(["a"]) == True
    # Groups []: NOT (F AND F) -> NOT F -> T
    assert filter_func([]) == True

# %%
#|export
def test_nested_parens():
    """Nested parentheses work correctly."""
    filter_func = get_group_filter_func("((a OR b) AND c)")
    assert filter_func(["a", "c"]) == True
    assert filter_func(["b", "c"]) == True
    assert filter_func(["c"]) == False

# %%
#|export
def test_deeply_nested_parens():
    """Deeply nested parentheses."""
    filter_func = get_group_filter_func("((a AND (b OR c)) OR (d AND e))")
    # (a AND (b OR c)) OR (d AND e)
    assert filter_func(["a", "b"]) == True      # (T AND T) OR F -> T
    assert filter_func(["a", "c"]) == True      # (T AND T) OR F -> T
    assert filter_func(["d", "e"]) == True      # F OR T -> T
    assert filter_func(["a"]) == False          # (T AND F) OR F -> F
    assert filter_func(["d"]) == False          # F OR F -> F

# %%
#|export
def test_complex_expression_with_all_operators():
    """Complex expression using all operators and parentheses."""
    filter_func = get_group_filter_func("(backend OR frontend) AND NOT (deprecated OR legacy)")
    # True cases: has backend or frontend, and doesn't have deprecated or legacy
    assert filter_func(["backend"]) == True
    assert filter_func(["frontend"]) == True
    assert filter_func(["backend", "api"]) == True
    # False cases: missing backend/frontend
    assert filter_func(["api"]) == False
    # False cases: has deprecated or legacy
    assert filter_func(["backend", "deprecated"]) == False
    assert filter_func(["frontend", "legacy"]) == False

# %% [markdown]
# ## 7. Whitespace Handling

# %%
#|export
def test_extra_spaces_between_tokens():
    """Extra spaces between tokens are handled."""
    filter_func = get_group_filter_func("a  AND  b")
    assert filter_func(["a", "b"]) == True
    assert filter_func(["a"]) == False

# %%
#|export
def test_leading_trailing_spaces():
    """Leading and trailing spaces are handled."""
    filter_func = get_group_filter_func("  a AND b  ")
    assert filter_func(["a", "b"]) == True

# %%
#|export
def test_spaces_around_parens():
    """Spaces around parentheses are handled."""
    filter_func = get_group_filter_func("( a OR b ) AND c")
    assert filter_func(["a", "c"]) == True

# %%
#|export
def test_no_spaces_around_parens():
    """No spaces around parentheses works."""
    filter_func = get_group_filter_func("(a OR b)AND c")
    assert filter_func(["a", "c"]) == True

# %% [markdown]
# ## 8. Case Insensitivity (Operators Only)

# %%
#|export
def test_lowercase_and():
    """Lowercase 'and' operator works."""
    filter_func = get_group_filter_func("a and b")
    assert filter_func(["a", "b"]) == True

# %%
#|export
def test_lowercase_or():
    """Lowercase 'or' operator works."""
    filter_func = get_group_filter_func("a or b")
    assert filter_func(["a"]) == True

# %%
#|export
def test_lowercase_not():
    """Lowercase 'not' operator works."""
    filter_func = get_group_filter_func("not a")
    assert filter_func(["b"]) == True

# %%
#|export
def test_mixed_case_operators():
    """Mixed case operators work."""
    filter_func = get_group_filter_func("a And b Or c")
    assert filter_func(["a", "b"]) == True
    assert filter_func(["c"]) == True

# %% [markdown]
# ## 9. Group Name Handling
#
# Group names can contain: alphanumeric, underscore, hyphen, forward slash

# %%
#|export
def test_simple_alphanumeric_names():
    """Simple alphanumeric group names."""
    filter_func = get_group_filter_func("backend123")
    assert filter_func(["backend123"]) == True

# %%
#|export
def test_hyphenated_names():
    """Hyphenated group names work."""
    filter_func = get_group_filter_func("my-group AND other-group")
    assert filter_func(["my-group", "other-group"]) == True

# %%
#|export
def test_underscored_names():
    """Underscored group names work."""
    filter_func = get_group_filter_func("my_group AND other_group")
    assert filter_func(["my_group", "other_group"]) == True

# %%
#|export
def test_slashed_names():
    """Slashed group names (hierarchical) work."""
    filter_func = get_group_filter_func("category/subcategory")
    assert filter_func(["category/subcategory"]) == True

# %%
#|export
def test_complex_group_names():
    """Complex group names with mixed characters."""
    filter_func = get_group_filter_func("my-project_v2/prod AND api-v3")
    assert filter_func(["my-project_v2/prod", "api-v3"]) == True

# %%
#|export
def test_numeric_group_names():
    """Purely numeric group names work."""
    filter_func = get_group_filter_func("2024 AND v2")
    assert filter_func(["2024", "v2"]) == True

# %%
#|export
def test_single_char_names():
    """Single character group names work."""
    filter_func = get_group_filter_func("a AND b")
    assert filter_func(["a", "b"]) == True

# %% [markdown]
# ## 10. Error Cases

# %%
#|export
def test_empty_expression_raises():
    """Empty expression raises ValueError."""
    with pytest.raises(ValueError, match="Empty expression"):
        get_group_filter_func("")

# %%
#|export
def test_whitespace_only_expression_raises():
    """Whitespace-only expression raises ValueError."""
    with pytest.raises(ValueError, match="Empty expression"):
        get_group_filter_func("   ")

# %%
#|export
def test_unmatched_open_paren_raises():
    """Unmatched opening parenthesis raises ValueError."""
    with pytest.raises(ValueError, match="[Uu]nmatched"):
        filter_func = get_group_filter_func("(a AND b")
        filter_func(["a", "b"])

# %%
#|export
def test_unmatched_close_paren_raises():
    """Unmatched closing parenthesis raises ValueError."""
    with pytest.raises(ValueError, match="[Uu]nexpected"):
        filter_func = get_group_filter_func("a AND b)")
        filter_func(["a", "b"])

# %%
#|export
def test_trailing_and_raises():
    """Trailing AND operator raises ValueError."""
    with pytest.raises(ValueError):
        filter_func = get_group_filter_func("a AND")
        filter_func(["a"])

# %%
#|export
def test_trailing_or_raises():
    """Trailing OR operator raises ValueError."""
    with pytest.raises(ValueError):
        filter_func = get_group_filter_func("a OR")
        filter_func(["a"])

# %%
#|export
def test_leading_and_raises():
    """Leading AND operator raises ValueError."""
    with pytest.raises(ValueError):
        filter_func = get_group_filter_func("AND a")
        filter_func(["a"])

# %%
#|export
def test_leading_or_raises():
    """Leading OR operator raises ValueError."""
    with pytest.raises(ValueError):
        filter_func = get_group_filter_func("OR a")
        filter_func(["a"])

# %%
#|export
def test_double_and_raises():
    """Double AND operator raises ValueError."""
    with pytest.raises(ValueError):
        filter_func = get_group_filter_func("a AND AND b")
        filter_func(["a", "b"])

# %%
#|export
def test_double_or_raises():
    """Double OR operator raises ValueError."""
    with pytest.raises(ValueError):
        filter_func = get_group_filter_func("a OR OR b")
        filter_func(["a", "b"])

# %%
#|export
def test_invalid_character_raises():
    """Invalid character in expression raises ValueError."""
    with pytest.raises(ValueError, match="Invalid character"):
        get_group_filter_func("a & b")

# %%
#|export
def test_pipe_character_raises():
    """Pipe character raises ValueError."""
    with pytest.raises(ValueError, match="Invalid character"):
        get_group_filter_func("a | b")

# %%
#|export
def test_empty_parens_raises():
    """Empty parentheses raises ValueError."""
    with pytest.raises(ValueError):
        filter_func = get_group_filter_func("a AND ()")
        filter_func(["a"])

# %% [markdown]
# ## 11. Edge Cases

# %%
#|export
def test_group_name_resembling_and():
    """Group name that starts like 'and' but isn't.

    The tokenizer correctly requires word boundaries for operators,
    so 'android' is treated as an identifier, not 'AND' + 'roid'.
    """
    filter_func = get_group_filter_func("android AND api")
    assert filter_func(["android", "api"]) == True
    assert filter_func(["android"]) == False
    assert filter_func(["api"]) == False

# %%
#|export
def test_group_name_resembling_or():
    """Group name that starts like 'or' but isn't.

    The tokenizer correctly requires word boundaries for operators,
    so 'oracle' is treated as an identifier, not 'OR' + 'acle'.
    """
    filter_func = get_group_filter_func("oracle AND api")
    assert filter_func(["oracle", "api"]) == True
    assert filter_func(["oracle"]) == False
    assert filter_func(["api"]) == False

# %%
#|export
def test_group_name_resembling_not():
    """Group name that starts like 'not' but isn't.

    The tokenizer correctly requires word boundaries for operators,
    so 'notebook' is treated as an identifier, not 'NOT' + 'ebook'.
    """
    filter_func = get_group_filter_func("notebook AND api")
    assert filter_func(["notebook", "api"]) == True
    assert filter_func(["notebook"]) == False
    assert filter_func(["api"]) == False

# %%
#|export
def test_filter_func_is_reusable():
    """Filter function can be called multiple times."""
    filter_func = get_group_filter_func("a AND b")
    assert filter_func(["a", "b"]) == True
    assert filter_func(["a"]) == False
    assert filter_func(["a", "b", "c"]) == True
    assert filter_func([]) == False

# %%
#|export
def test_filter_func_does_not_modify_input():
    """Filter function doesn't modify input list."""
    groups = ["a", "b"]
    original = groups.copy()
    filter_func = get_group_filter_func("a AND b")
    filter_func(groups)
    assert groups == original

# %%
#|export
def test_groups_not_in_expression():
    """Groups not mentioned in expression don't affect result."""
    filter_func = get_group_filter_func("a AND b")
    assert filter_func(["a", "b", "c", "d", "e"]) == True

# %%
#|export
def test_case_sensitive_group_names():
    """Group names are case-sensitive."""
    filter_func = get_group_filter_func("Backend")
    assert filter_func(["Backend"]) == True
    assert filter_func(["backend"]) == False
    assert filter_func(["BACKEND"]) == False

# %% [markdown]
# ## 12. Real-World Scenarios

# %%
#|export
def test_scenario_backend_not_deprecated():
    """Real scenario: backend services not deprecated."""
    filter_func = get_group_filter_func("backend AND NOT deprecated")
    assert filter_func(["backend", "api"]) == True
    assert filter_func(["backend", "deprecated"]) == False
    assert filter_func(["frontend"]) == False

# %%
#|export
def test_scenario_multiple_environments():
    """Real scenario: prod or staging, not legacy."""
    filter_func = get_group_filter_func("(prod OR staging) AND NOT legacy")
    assert filter_func(["prod", "backend"]) == True
    assert filter_func(["staging", "frontend"]) == True
    assert filter_func(["prod", "legacy"]) == False
    assert filter_func(["dev"]) == False

# %%
#|export
def test_scenario_hierarchical_groups():
    """Real scenario: hierarchical group filtering."""
    filter_func = get_group_filter_func("company/team-a OR company/team-b")
    assert filter_func(["company/team-a"]) == True
    assert filter_func(["company/team-b"]) == True
    assert filter_func(["company/team-c"]) == False

# %%
#|export
def test_scenario_complex_project_filter():
    """Real scenario: complex project filtering."""
    expr = "(backend OR frontend) AND (prod OR staging) AND NOT (deprecated OR archived)"
    filter_func = get_group_filter_func(expr)
    # Should match
    assert filter_func(["backend", "prod"]) == True
    assert filter_func(["frontend", "staging"]) == True
    assert filter_func(["backend", "frontend", "prod"]) == True
    # Should not match - missing environment
    assert filter_func(["backend"]) == False
    # Should not match - deprecated
    assert filter_func(["backend", "prod", "deprecated"]) == False
    # Should not match - archived
    assert filter_func(["frontend", "staging", "archived"]) == False

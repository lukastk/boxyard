# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _utils.logical_expressions

# %%
#|default_exp _utils.logical_expressions

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();
import boxyard._utils.logical_expressions as this_module

# %%
#|hide
show_doc(this_module._tokenize_expression)

# %%
# "a".isalnum?

# %%
#|exporti
def _is_identifier_char(c: str) -> bool:
    """Check if a character can be part of an identifier (group name)."""
    return c.isalnum() or c in "_-/"


def _tokenize_expression(expression: str) -> list[str]:
    """Tokenize the expression into operators, identifiers, and parentheses."""
    tokens = []
    i = 0
    expression = expression.strip()

    while i < len(expression):
        # Skip whitespace
        if expression[i].isspace():
            i += 1
            continue

        # Check for operators - must be followed by non-identifier char (word boundary)
        if expression[i : i + 3].upper() == "AND" and (
            i + 3 >= len(expression) or not _is_identifier_char(expression[i + 3])
        ):
            tokens.append("AND")
            i += 3
        elif expression[i : i + 2].upper() == "OR" and (
            i + 2 >= len(expression) or not _is_identifier_char(expression[i + 2])
        ):
            tokens.append("OR")
            i += 2
        elif expression[i : i + 3].upper() == "NOT" and (
            i + 3 >= len(expression) or not _is_identifier_char(expression[i + 3])
        ):
            tokens.append("NOT")
            i += 3
        elif expression[i] == "(":
            tokens.append("(")
            i += 1
        elif expression[i] == ")":
            tokens.append(")")
            i += 1
        else:
            # Read identifier (group name)
            start = i
            while i < len(expression) and _is_identifier_char(expression[i]):
                i += 1
            if i == start:
                raise ValueError(f"Invalid character at position {i}: {expression[i]}")
            tokens.append(expression[start:i])

    return tokens


def _parse_or_expression(
    tokens: list[str], pos: list[int], box_groups: set[str]
) -> bool:
    """Parse OR expressions (lowest precedence)."""
    left = _parse_and_expression(tokens, pos, box_groups)

    while pos[0] < len(tokens) and tokens[pos[0]] == "OR":
        pos[0] += 1
        right = _parse_and_expression(tokens, pos, box_groups)
        left = left or right

    return left


def _parse_and_expression(
    tokens: list[str], pos: list[int], box_groups: set[str]
) -> bool:
    """Parse AND expressions (medium precedence)."""
    left = _parse_not_expression(tokens, pos, box_groups)

    while pos[0] < len(tokens) and tokens[pos[0]] == "AND":
        pos[0] += 1
        right = _parse_not_expression(tokens, pos, box_groups)
        left = left and right

    return left


def _parse_not_expression(
    tokens: list[str], pos: list[int], box_groups: set[str]
) -> bool:
    """Parse NOT expressions and atoms (highest precedence)."""
    if pos[0] >= len(tokens):
        raise ValueError("Unexpected end of expression")

    # Handle NOT operator
    if tokens[pos[0]] == "NOT":
        pos[0] += 1
        return not _parse_not_expression(tokens, pos, box_groups)

    # Handle parentheses
    if tokens[pos[0]] == "(":
        pos[0] += 1
        result = _parse_or_expression(tokens, pos, box_groups)
        if pos[0] >= len(tokens) or tokens[pos[0]] != ")":
            raise ValueError("Unmatched opening parenthesis")
        pos[0] += 1
        return result

    # Handle group name (identifier)
    if tokens[pos[0]] in ("AND", "OR", ")"):
        raise ValueError(f"Unexpected operator or parenthesis: {tokens[pos[0]]}")

    group_name = tokens[pos[0]]
    pos[0] += 1
    return group_name in box_groups

# %%
_tokenize_expression("group1 AND (group2 OR group3)")

# %%
_tokenize_expression("group1 AND parent_group/child_group")

# %%
#|hide
show_doc(this_module.get_group_filter_func)

# %%
#|export
def get_group_filter_func(expression: str) -> bool:
    """
    Get a function that evaluates a boolean expression against a set of box groups.

    Supports AND, OR, NOT operators and parentheses for grouping.
    Operator precedence: NOT > AND > OR

    Examples:
        "group1 AND group2"
        "group1 OR group2"
        "NOT group1"
        "group1 AND (group2 OR group3)"
        "(group1 OR group2) AND NOT group3"

    Args:
        expression: Boolean expression string

    Returns:
        Function that takes a set of box groups and returns True if the expression evaluates to True for the given groups, False otherwise

    Raises:
        ValueError: If the expression is invalid or contains syntax errors
    """
    # Tokenize the expression
    tokens = _tokenize_expression(expression)
    if not tokens:
        raise ValueError("Empty expression")

    def _filter_func(box_groups: set[str] | list[str]) -> bool:
        if isinstance(box_groups, list):
            box_groups = set(box_groups)

        # Parse and evaluate
        pos = [0]  # Use list to allow modification in nested calls
        result = _parse_or_expression(tokens, pos, box_groups)

        # Check if we consumed all tokens
        if pos[0] < len(tokens):
            raise ValueError(f"Unexpected token at position {pos[0]}: {tokens[pos[0]]}")

        return result

    return _filter_func

# %%
#|exporti
def _evaluate_group_expression(
    expression: str, box_groups: set[str] | list[str]
) -> bool:
    _filter_func = get_group_filter_func(expression)
    return _filter_func(box_groups)

# %%
# Example usage:
box_groups = {"group1", "group2"}

# Simple expressions
assert _evaluate_group_expression("group1", box_groups) == True
assert _evaluate_group_expression("group3", box_groups) == False
assert _evaluate_group_expression("group1 AND group2", box_groups) == True
assert _evaluate_group_expression("group1 OR group3", box_groups) == True
assert _evaluate_group_expression("NOT group1", box_groups) == False

# Complex expressions
assert _evaluate_group_expression("group1 AND (group2 OR group3)", box_groups) == True
assert (
    _evaluate_group_expression("(group1 OR group2) AND NOT group3", box_groups) == True
)
assert _evaluate_group_expression("group1 AND NOT group2", box_groups) == False
assert _evaluate_group_expression("group1 AND (NOT group2)", box_groups) == False

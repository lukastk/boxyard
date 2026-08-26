# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `multi-sync`'s Status Colours Have To Name Real Statuses
#
# `get_status_lines` colours each box's line by looking its status up in two
# dicts. A key that is not a status any box ever HAS is not an error: rich
# accepts the resulting `[bold ]` without complaint and renders the text bold
# and uncoloured, so the miss is invisible.
#
# That is what happened. The map said `"Syncing"`; `_task` sets `"Syncing..."`.
# Every in-flight line on the live board lost its colour and nothing said so.
#
# This walks the module's source and checks both maps against the statuses the
# code can actually produce. `name_color` deliberately has NO in-flight entry —
# a box's name stays plain until it has an outcome — so the test asserts only
# that the keys present are real, never that any particular key exists.

# %%
#|default_exp unit._cli.test_multi_sync_status_colours

# %%
#|export
import ast
import inspect

import pytest

from boxyard._cli import multi_sync as _multi_sync_module

# %% [markdown]
# ## Extracting the statuses and the colour keys

# %%
#|export
_SOURCE = inspect.getsource(_multi_sync_module)
_TREE = ast.parse(_SOURCE)


def _assigned_statuses() -> set[str]:
    """Every status string the module assigns into `sync_stats`.

    The status is the second element of the tuple, and one site builds it with
    a nested conditional -- so every string constant inside that element
    counts, not just a top-level literal.
    """
    out: set[str] = set()
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            if not (isinstance(target.value, ast.Name) and target.value.id == "sync_stats"):
                continue
            if not isinstance(node.value, ast.Tuple) or len(node.value.elts) < 2:
                continue
            for sub in ast.walk(node.value.elts[1]):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.add(sub.value)
    return out


def _colour_map_keys() -> dict[str, set[str]]:
    """The keys of every `<name>_color = {...}` dict literal in the module.

    The dict is not the assigned value -- it is the receiver of a `.get()`
    (`status_color = {...}.get(sync_stat, "")`), which is the whole reason a
    missing key degrades silently instead of raising. So this looks for the
    dict ANYWHERE inside the assigned expression.
    """
    out: dict[str, set[str]] = {}
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not names or not names[0].endswith("_color"):
            continue
        for sub in ast.walk(node.value):
            if not isinstance(sub, ast.Dict):
                continue
            keys = set()
            for key in sub.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
            out[names[0]] = keys
            break
    return out

# %% [markdown]
# ## The extraction finds something
#
# A walk that silently matched nothing would make the whole test vacuous.

# %%
#|export
def test_extraction_is_not_vacuous():
    statuses = _assigned_statuses()
    assert "Syncing..." in statuses, statuses
    assert {"Success", "Error", "Interrupted"} <= statuses, statuses
    maps = _colour_map_keys()
    assert set(maps) == {"status_color", "name_color"}, sorted(maps)

# %% [markdown]
# ## Every colour key is a real status

# %%
#|export
@pytest.mark.parametrize("map_name", sorted(_colour_map_keys()))
def test_colour_keys_are_real_statuses(map_name):
    statuses = _assigned_statuses()
    unknown = sorted(_colour_map_keys()[map_name] - statuses)
    assert not unknown, (
        f"{map_name} has keys that are not statuses any box ever has: {unknown}. "
        f"rich renders the miss as an empty style rather than failing, so the "
        f"line just loses its colour. Real statuses: {sorted(statuses)}"
    )

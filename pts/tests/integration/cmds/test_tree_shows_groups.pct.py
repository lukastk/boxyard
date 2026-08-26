# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `boxyard tree` Has Never Shown a Box's Groups
#
# The label was built as a rich MARKUP string:
#
# ```python
# groups_str = f" [groups: {', '.join(bm.groups)}]" if bm.groups else ""
# return f"{status}{bm.name} ({bm.box_id}){groups_str}"
# ```
#
# rich parses `[...]` as a style tag, so the whole suffix was swallowed — the
# only trace left was a stray trailing space where the groups should have been.
# Both `boxyard tree` and `boxyard list --view tree` did it.
#
# `list --view groups` escaped its own bracketed suffix by hand (`\[`), which is
# what makes the other two an oversight rather than a choice — but it did not
# escape the box or GROUP NAMES, and a name containing `[` is perfectly legal
# (`validate_box_name` forbids only path separators and the like).

# %%
#|default_exp integration.cmds.test_tree_shows_groups

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import tomli_w

from boxyard.cmds import init_boxyard, modify_boxmeta, new_box


def _yard(tmp_path: Path) -> Path:
    config_path = tmp_path / ".config" / "boxyard" / "config.toml"
    init_boxyard(config_path=config_path, data_path=tmp_path / ".boxyard", verbose=False)
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    data["user_boxes_path"] = (tmp_path / "user_boxes").as_posix()
    data["user_box_groups_path"] = (tmp_path / "user_box_groups").as_posix()
    config_path.write_text(tomli_w.dumps(data))
    return config_path


def _run(config_path: Path, *args: str) -> str:
    """argv, never a shell string."""
    cli = Path(sys.executable).parent / "boxyard"
    res = subprocess.run(
        [str(cli) if cli.exists() else "boxyard", "--config", str(config_path), *args],
        capture_output=True, text=True, env={"COLUMNS": "200", **_clean_env()},
    )
    assert res.returncode == 0, f"exit {res.returncode}\n{res.stdout}\n{res.stderr}"
    return res.stdout


def _clean_env():
    import os
    env = dict(os.environ)
    # This machine exports a DEFAULT_BOX_GROUPS; a test yard must not inherit it.
    env["DEFAULT_BOX_GROUPS"] = ""
    return env

# %% [markdown]
# ## The groups appear in `tree` and in `list --view tree`

# %%
#|export
@pytest.mark.integration
@pytest.mark.parametrize("args", [("tree",), ("list", "--view", "tree")])
def test_tree_shows_groups(tmp_path, args):
    config_path = _yard(tmp_path)
    index_name = new_box(config_path=config_path, box_name="grouped", initialise_git=False)
    modify_boxmeta(
        config_path=config_path,
        box_index_name=index_name,
        modifications={"groups": ["alpha", "beta"]},
    )

    # TESTREF: test_tree_shows_groups
    out = _run(config_path, *args)
    assert "groups: alpha, beta" in out, (
        f"the groups were swallowed by rich's markup parser:\n{out}"
    )

# %% [markdown]
# ## A box name containing a bracket survives every view
#
# `[` is legal in a box name, and an unescaped markup string mangles it.

# %%
#|export
@pytest.mark.integration
@pytest.mark.parametrize(
    "args", [("tree",), ("list", "--view", "tree"), ("list", "--view", "groups")]
)
def test_bracketed_box_name_survives(tmp_path, args):
    config_path = _yard(tmp_path)
    index_name = new_box(config_path=config_path, box_name="weird[name]", initialise_git=False)
    modify_boxmeta(
        config_path=config_path, box_index_name=index_name, modifications={"groups": ["g"]}
    )

    out = _run(config_path, *args)
    assert "weird[name]" in out, f"the box name was mangled:\n{out}"

# %% [markdown]
# ## The "[unknown parent]" header is not swallowed either
#
# It was a markup string too, so the orphan branch showed a bare `└── ` with
# nothing to explain what was under it.

# %%
#|export
@pytest.mark.integration
def test_unknown_parent_header_is_shown(tmp_path):
    config_path = _yard(tmp_path)
    parent = new_box(config_path=config_path, box_name="the-parent", initialise_git=False)
    child = new_box(config_path=config_path, box_name="the-child", initialise_git=False)

    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    config = get_config(config_path)
    parent_id = get_boxyard_meta(config, force_create=True).by_index_name[parent].box_id
    modify_boxmeta(
        config_path=config_path, box_index_name=child, modifications={"parents": [parent_id]}
    )

    # Rooting at the CHILD leaves the parent outside the shown set, so it lands
    # under the orphan branch.
    # TESTREF: test_unknown_parent_header_is_shown
    out = _run(config_path, "tree", "--root", "the-child")
    assert "[unknown parent]" in out, f"the orphan header was swallowed:\n{out}"

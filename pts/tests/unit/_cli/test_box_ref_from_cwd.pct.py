# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # A Bare Command Means "The Box I Am Standing In"
#
# `_get_box_index_name` carried a cwd-inference fallback whose error message
# read *"Box not specified and could not be inferred from current working
# directory."* — and it was **unreachable**. With no selector the function
# always entered the picker branch first, so `boxyard sync` typed inside a box
# opened an fzf picker over the whole yard instead of syncing that box.
#
# The inference now runs *before* the picker. The candidate check is
# load-bearing: several callers pass a filtered `box_metas` (`include` passes
# only excluded boxes, `exclude` only eligible ones, `path` a group-filtered
# set), and a cwd box outside that set is not a valid answer for the command.

# %%
#|default_exp unit._cli.test_box_ref_from_cwd

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import tomllib
from pathlib import Path

import tomli_w

from boxyard._cli.app import app_state
from boxyard._cli.main import _get_box_index_name
from boxyard._models import BoxPart, get_boxyard_meta
from boxyard.cmds import init_boxyard, new_box
from boxyard.config import get_config


def _yard(tmp_path: Path) -> Path:
    config_path = tmp_path / ".config" / "boxyard" / "config.toml"
    init_boxyard(config_path=config_path, data_path=tmp_path / ".boxyard", verbose=False)
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    data["user_boxes_path"] = (tmp_path / "user_boxes").as_posix()
    data["user_box_groups_path"] = (tmp_path / "user_box_groups").as_posix()
    config_path.write_text(tomli_w.dumps(data))
    return config_path


def _data_path(config_path: Path, index_name: str) -> Path:
    config = get_config(config_path)
    box_meta = get_boxyard_meta(config, force_create=True).by_index_name[index_name]
    return box_meta.get_local_part_path(config, BoxPart.DATA)


def _forbid_picker(monkeypatch):
    """Any picker call is a failure: these cases must never reach one."""

    def _explode(*args, **kwargs):
        raise AssertionError("a picker was opened instead of using the cwd box")

    monkeypatch.setattr("boxyard._utils.base.run_fzf", _explode)

# %% [markdown]
# ## Inside a box, with no selector, that box is used

# %%
#|export
def test_bare_invocation_uses_the_cwd_box(tmp_path, monkeypatch):
    config_path = _yard(tmp_path)
    monkeypatch.setitem(app_state, "config_path", config_path)
    _forbid_picker(monkeypatch)

    first = new_box(config_path=config_path, box_name="alpha", initialise_git=False)
    new_box(config_path=config_path, box_name="beta", initialise_git=False)

    # A DEEP subdirectory, because the inference walks up to the box root.
    deep = _data_path(config_path, first) / "sub" / "deeper"
    deep.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(deep)

    # TESTREF: test_bare_invocation_uses_the_cwd_box
    got = _get_box_index_name(
        box_name=None, box_id=None, box_index_name=None,
        name_match_mode=None, name_match_case=False,
    )
    assert got == first

# %% [markdown]
# ## A cwd box outside the candidate set falls through

# %%
#|export
def test_cwd_box_outside_the_candidate_set_is_not_used(tmp_path, monkeypatch):
    config_path = _yard(tmp_path)
    monkeypatch.setitem(app_state, "config_path", config_path)

    first = new_box(config_path=config_path, box_name="alpha", initialise_git=False)
    second = new_box(config_path=config_path, box_name="beta", initialise_git=False)

    monkeypatch.chdir(_data_path(config_path, first))

    config = get_config(config_path)
    boxyard_meta = get_boxyard_meta(config, force_create=True)

    # Only the OTHER box is a candidate, so the cwd box is not a valid answer.
    got = _get_box_index_name(
        box_name=None, box_id=None, box_index_name=None,
        name_match_mode=None, name_match_case=False,
        box_metas=[boxyard_meta.by_index_name[second]],
    )
    assert got == second

# %% [markdown]
# ## Outside any box, a bare invocation still reaches the picker

# %%
#|export
def test_outside_a_box_falls_through_to_the_picker(tmp_path, monkeypatch):
    config_path = _yard(tmp_path)
    monkeypatch.setitem(app_state, "config_path", config_path)

    new_box(config_path=config_path, box_name="alpha", initialise_git=False)
    new_box(config_path=config_path, box_name="beta", initialise_git=False)

    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    monkeypatch.chdir(outside)

    calls = []

    def _fake_fzf(terms, disp_terms=None):
        calls.append(list(terms))
        return 0, terms[0]

    monkeypatch.setattr("boxyard._utils.base.run_fzf", _fake_fzf)

    got = _get_box_index_name(
        box_name=None, box_id=None, box_index_name=None,
        name_match_mode=None, name_match_case=False,
    )
    assert calls, "the picker was not reached from outside a box"
    assert got == calls[0][0]

# %% [markdown]
# ## An explicit selector still wins over the cwd

# %%
#|export
def test_an_explicit_selector_beats_the_cwd(tmp_path, monkeypatch):
    config_path = _yard(tmp_path)
    monkeypatch.setitem(app_state, "config_path", config_path)
    _forbid_picker(monkeypatch)

    first = new_box(config_path=config_path, box_name="alpha", initialise_git=False)
    second = new_box(config_path=config_path, box_name="beta", initialise_git=False)

    monkeypatch.chdir(_data_path(config_path, first))

    got = _get_box_index_name(
        box_name="beta", box_id=None, box_index_name=None,
        name_match_mode=None, name_match_case=False,
    )
    assert got == second

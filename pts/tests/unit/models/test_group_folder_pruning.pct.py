# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `~/g` Keeps No Empty Directories
#
# `create_user_box_group_symlinks` rebuilds `~/g` from scratch and then prunes
# what is left empty. That pruning used to carry an `is_group_folder` guard
# exempting directories whose path matched a GROUP NAME — and the guard was
# accidental rather than a policy.
#
# A group's directory is named after its `symlink_name` when it has one, and
# those are nested paths (`all/proj`, `active/all`). So the guard never matched
# on a config that sets them and matched on every group in a config that does
# not: the same code pruned empty group directories or kept them depending on a
# field that has nothing to do with pruning. Every group in this fleet's config
# sets one, so the real behaviour there was "prune everything", and that is the
# behaviour kept — now stated rather than arrived at.

# %%
#|default_exp unit.models.test_group_folder_pruning

# %%
#|export
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from boxyard._models import BoxMeta, create_user_box_group_symlinks
from boxyard.config import BoxGroupConfig, BoxGroupTitleMode

# %% [markdown]
# ## A yard with one populated group and two empty ones

# %%
#|export
@pytest.fixture
def yard(tmp_path, monkeypatch):
    """A groups directory, a config, and one box that is in `kept` only."""
    groups_path = tmp_path / "g"
    groups_path.mkdir()
    box_data = tmp_path / "boxes" / "20260822_aaaaa__only-box"
    box_data.mkdir(parents=True)

    config = MagicMock()
    config.user_box_groups_path = groups_path
    config.box_groups = {
        # Nested symlink names are what the real config uses, and they are why
        # the old guard never fired.
        "kept": BoxGroupConfig(symlink_name="all/kept", box_title_mode=BoxGroupTitleMode.NAME),
        "empty-sibling": BoxGroupConfig(symlink_name="all/empty-sibling"),
        # No symlink_name: the ONLY shape the old guard protected.
        "plain-empty": BoxGroupConfig(),
    }
    config.virtual_box_groups = {}

    box = BoxMeta(
        creation_timestamp_utc="20260822_000000",
        box_subid="aaaaa",
        name="only-box",
        storage_location="local",
        creator_hostname="test",
        groups=["kept"],
    )
    monkeypatch.setattr(BoxMeta, "check_included", lambda self, config: True)
    monkeypatch.setattr(
        BoxMeta, "get_local_part_path", lambda self, config, part: box_data
    )
    monkeypatch.setattr(
        "boxyard._models.get_boxyard_meta",
        lambda config, *a, **k: MagicMock(box_metas=[box]),
    )

    # LEFTOVERS from a previous run, and the reason this fixture has to create
    # them by hand: a directory is only ever made as the parent of a symlink,
    # so a group that has never had a box here has no directory to prune. The
    # pruning only decides what happens to a group that EMPTIES OUT — its last
    # box excluded, deleted or regrouped. Without these two lines the
    # assertions below pass whatever the code does.
    (groups_path / "all" / "empty-sibling").mkdir(parents=True)
    (groups_path / "plain-empty").mkdir()

    return config, groups_path

# %% [markdown]
# ## A group that has emptied out does not keep its directory
#
# Neither shape survives: the one whose `symlink_name` is nested, nor the one
# with no `symlink_name` at all, which is the case the old guard exempted.

# %%
#|export
def test_empty_group_directories_are_pruned(yard):
    config, groups_path = yard
    create_user_box_group_symlinks(config)

    assert (groups_path / "all" / "kept" / "only-box").is_symlink()
    assert not (groups_path / "all" / "empty-sibling").exists()
    # TESTREF: the shape the removed `is_group_folder` guard used to protect.
    assert not (groups_path / "plain-empty").exists()

# %% [markdown]
# ## A parent survives while any child is populated
#
# Pruning is deepest-first, so `all/` has to be judged AFTER `all/kept` has
# been kept and `all/empty-sibling` removed — not before.

# %%
#|export
def test_parent_of_a_populated_group_survives(yard):
    config, groups_path = yard
    create_user_box_group_symlinks(config)

    assert (groups_path / "all").is_dir()
    assert sorted(p.name for p in (groups_path / "all").iterdir()) == ["kept"]

# %% [markdown]
# ## Debris left by a previous layout is cleaned up
#
# `~/g` is rebuilt on every run, so a directory left behind by a group that has
# been renamed or deleted is exactly the thing pruning exists to remove.

# %%
#|export
def test_stale_directories_are_removed(yard):
    config, groups_path = yard
    (groups_path / "was-a-group-once").mkdir()
    (groups_path / "deep" / "nested" / "debris").mkdir(parents=True)

    create_user_box_group_symlinks(config)

    assert not (groups_path / "was-a-group-once").exists()
    assert not (groups_path / "deep").exists()

# %% [markdown]
# ## A dotfile is not emptiness
#
# The walk skips dotted entries, so a directory holding only a `.DS_Store` or a
# `.gitkeep` counts as empty and goes. That is deliberate — but a dotfile at
# the TOP of the groups path is skipped entirely and must survive.

# %%
#|export
def test_top_level_dotfiles_are_left_alone(yard):
    config, groups_path = yard
    (groups_path / ".keep-me").mkdir()

    create_user_box_group_symlinks(config)

    assert (groups_path / ".keep-me").is_dir()

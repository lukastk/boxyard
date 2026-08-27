# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Turning The Merge On Costs Nothing Until Something Diverges
#
# `merge_diverged_boxmetas` must not make an ordinary sync pass more expensive.
#
# The first version of it asked, before every META sync, whether a merge was
# needed — and that question is `get_sync_status`, which makes TWO remote calls.
# On the largest yard in this fleet that is 1,180 extra round trips every 20
# minutes, for boxes that have nothing wrong with them.
#
# This repo has already been bitten by exactly that shape. From `multi_sync`:
#
# > Fetch the tombstones ONCE, not once per box. Asked per box that is one SFTP
# > connection each — 587 per pass, per machine, every 20 minutes, which
# > saturated the storage box's connection limit and was failing ~8 boxes per
# > pass on three machines.
#
# The merge is attempted from the sync's FAILURE path instead, so a box that
# has not diverged pays nothing. This test pins that: the same sync, with the
# setting on and off, must make the same number of remote calls.

# %%
#|default_exp integration.cmds.test_merge_costs_nothing_when_clean

# %%
#|export
import asyncio

import pytest
import tomli_w
import tomllib

from boxyard.cmds import new_box, sync_box
from boxyard.config import get_config

# %% [markdown]
# ## Counting what reaches the remote

# %%
#|export
@pytest.fixture
def count_rclone(monkeypatch):
    """Count every rclone invocation the sync makes."""
    import boxyard._utils.base as _base

    calls = []
    original = _base.run_cmd_async

    async def counting(cmd, *a, **k):
        calls.append(cmd)
        return await original(cmd, *a, **k)

    monkeypatch.setattr(_base, "run_cmd_async", counting)
    return calls


def _set_flag(config_path, value):
    cfg = tomllib.loads(config_path.read_text())
    cfg["merge_diverged_boxmetas"] = value
    config_path.write_text(tomli_w.dumps(cfg))


# %% [markdown]
# ## The same sync, with the setting on and off

# %%
#|export
@pytest.mark.integration
def test_a_clean_sync_costs_the_same_either_way(temp_boxyard, count_rclone):
    remote_name, _, _, config_path, _ = temp_boxyard
    index_name = new_box(
        config_path=config_path, box_name="untroubled", storage_location=remote_name
    )
    # First sync: pushes, and records the merge base.
    asyncio.run(sync_box(config_path=config_path, box_index_name=index_name))

    _set_flag(config_path, False)
    assert get_config(config_path).merge_diverged_boxmetas is False
    count_rclone.clear()
    asyncio.run(sync_box(config_path=config_path, box_index_name=index_name))
    without = len(count_rclone)

    _set_flag(config_path, True)
    assert get_config(config_path).merge_diverged_boxmetas is True
    count_rclone.clear()
    asyncio.run(sync_box(config_path=config_path, box_index_name=index_name))
    with_flag = len(count_rclone)

    assert without > 0, "the counter caught nothing, so this proves nothing"
    assert with_flag == without, (
        f"turning the merge on cost {with_flag - without} extra remote calls on a "
        f"box that has not diverged; across a 590-box yard every 20 minutes that "
        f"is the failure mode that saturated the storage box once already"
    )

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Merging a Real Divergence
#
# The merge function is unit-tested exhaustively; this is the part that cannot
# be: that a boxmeta two machines have actually edited, in two real yards
# sharing a remote, ends up carrying both edits — and that with the setting off
# it still refuses exactly as it does today.
#
# The scenario is the one that happened. Machine A adds a group locally and
# does not push (`add-to-group` does not push by default). Machine B claims the
# box, which writes `write_owner` and pushes. A's edit is now a two-sided
# divergence.

# %%
#|default_exp integration.cmds.test_meta_merge_end_to_end
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import asyncio

import pytest

from boxyard.cmds import new_box, sync_box, sync_missing_boxmetas, include_box, modify_boxmeta
from boxyard._models import get_boxyard_meta, BoxPart, SyncCondition, get_sync_status
from boxyard.config import get_config
from boxyard._utils.sync_helper import SyncUnsafe

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_meta_merge_end_to_end():
    asyncio.run(_test_meta_merge_end_to_end())

# %%
#|set_func_signature
async def _test_meta_merge_end_to_end(): ...

# %% [markdown]
# ## Two yards sharing one remote, with a box in both

# %%
#|export
(
    sl_name,
    sl_rclone_path,
    [(config1, config_path1, data_path1), (config2, config_path2, data_path2)],
) = create_boxyards(num_boxyards=2)

index_name = new_box(
    config_path=config_path1, box_name="both-edit-me", storage_location=sl_name
)
await sync_box(config_path=config_path1, box_index_name=index_name)
await sync_missing_boxmetas(config_path=config_path2)
await include_box(config_path=config_path2, box_index_name=index_name)
await sync_box(config_path=config_path2, box_index_name=index_name)

# Both sides now have a merge base: they have each synced this box's META.
config1 = get_config(config_path1)
config2 = get_config(config_path2)
box1 = get_boxyard_meta(config1, force_create=True).by_index_name[index_name]
box2 = get_boxyard_meta(config2, force_create=True).by_index_name[index_name]
assert box1.get_local_meta_base_path(config1).is_file()
assert box2.get_local_meta_base_path(config2).is_file()

_starting_groups = list(box1.groups)

# %% [markdown]
# ## Both sides edit, and only one pushes

# %%
#|export
# Yard 1 adds a group and does NOT push -- what `add-to-group` does by default.
modify_boxmeta(
    config_path=config_path1,
    box_index_name=index_name,
    modifications={"groups": _starting_groups + ["from-yard-1"]},
)

# Yard 2 adds a different one and pushes.
modify_boxmeta(
    config_path=config_path2,
    box_index_name=index_name,
    modifications={"groups": _starting_groups + ["from-yard-2"]},
)
await sync_box(config_path=config_path2, box_index_name=index_name)

# %% [markdown]
# ## With the setting off, this is a refusal — exactly as today

# %%
#|export
config1 = get_config(config_path1)
box1 = get_boxyard_meta(config1, force_create=True).by_index_name[index_name]
_status = await get_sync_status(
    rclone_config_path=config1.rclone_config_path,
    local_path=box1.get_local_part_path(config1, BoxPart.META),
    local_sync_record_path=box1.get_local_sync_record_path(config1, BoxPart.META),
    remote=sl_name,
    remote_path=(
        config1.storage_locations[sl_name].store_path
        / "boxes"
        / index_name
        / "boxmeta.toml"
    ),
    remote_sync_record_path=(
        config1.storage_locations[sl_name].store_path
        / "sync_records"
        / index_name
        / "meta.rec"
    ),
)
assert _status.sync_condition == SyncCondition.CONFLICT, _status.sync_condition

assert config1.merge_diverged_boxmetas is False
with pytest.raises(SyncUnsafe):
    await sync_box(config_path=config_path1, box_index_name=index_name)

# %% [markdown]
# ## With it on, both edits survive

# %%
#|export
# Turned on in the config FILE, not on a config object: `sync_box` reads the
# config from the path it is given, so an injected object would not reach it --
# and a test that patched around that would not be exercising the real switch.
import tomli_w as _tomli_w
import tomllib as _tomllib

_cfg = _tomllib.loads(config_path1.read_text())
_cfg["merge_diverged_boxmetas"] = True
config_path1.write_text(_tomli_w.dumps(_cfg))
assert get_config(config_path1).merge_diverged_boxmetas is True

await sync_box(config_path=config_path1, box_index_name=index_name)

_merged = get_boxyard_meta(config1, force_create=True).by_index_name[index_name]
assert "from-yard-1" in _merged.groups, _merged.groups
assert "from-yard-2" in _merged.groups, _merged.groups
for _g in _starting_groups:
    assert _g in _merged.groups

# %% [markdown]
# ## And the other machine converges on the same answer

# %%
#|export
await sync_box(config_path=config_path2, box_index_name=index_name)
_converged = get_boxyard_meta(get_config(config_path2), force_create=True).by_index_name[index_name]

# The same LIST, not merely the same set: two machines that agree on the
# content but not its order read each other's push as a change and trade the
# same boxmeta forever.
assert _converged.groups == _merged.groups, (_converged.groups, _merged.groups)

# %% [markdown]
# ## With the setting on but NO base, it still refuses
#
# The base is what the merge computes each side's delta against. Without one
# there is nothing to compare to, and a merge that proceeded anyway would be
# inventing the answer — the exact failure the whole design exists to avoid.
# Every box on the fleet was in this state on the day the base was introduced.

# %%
#|export
_second = new_box(
    config_path=config_path1, box_name="no-base-here", storage_location=sl_name
)
await sync_box(config_path=config_path1, box_index_name=_second)
await sync_missing_boxmetas(config_path=config_path2)
await include_box(config_path=config_path2, box_index_name=_second)
await sync_box(config_path=config_path2, box_index_name=_second)

modify_boxmeta(
    config_path=config_path1,
    box_index_name=_second,
    modifications={"groups": ["only-yard-1"]},
)
modify_boxmeta(
    config_path=config_path2,
    box_index_name=_second,
    modifications={"groups": ["only-yard-2"]},
)
await sync_box(config_path=config_path2, box_index_name=_second)

# Remove yard 1's base, as if this box had not synced since the upgrade.
config1 = get_config(config_path1)
_box = get_boxyard_meta(config1, force_create=True).by_index_name[_second]
_box.get_local_meta_base_path(config1).unlink()

assert config1.merge_diverged_boxmetas is True
# SyncUnsafe specifically, not `Exception`: the point is that it REFUSES, and
# a crash on the way to refusing would satisfy a broader assertion while
# meaning something entirely different.
with pytest.raises(SyncUnsafe):
    await sync_box(config_path=config_path1, box_index_name=_second)

# The local edit is untouched: declining must leave the divergence exactly as
# it was, not half-resolve it.
_after = get_boxyard_meta(get_config(config_path1), force_create=True).by_index_name[_second]
assert _after.groups == ["only-yard-1"], _after.groups

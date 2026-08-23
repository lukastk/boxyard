# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Tombstone Lookups Are Batched
#
# `sync_box` has to know whether a box was deleted from another machine. Asked
# per box that is one SFTP connection each — 587 per pass, per machine, every
# 20 minutes. On the live fleet that saturated the storage box's connection
# limit and was failing ~8 boxes per pass on three machines with "couldn't
# initialise SFTP".
#
# What matters here is not just that tombstones are still detected, but that
# the batched path makes ONE remote call rather than one per box, and that a
# failed lookup never degrades into "not tombstoned".

# %%
#|default_exp integration.cmds.test_tombstone_batching
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import pytest
import asyncio

import boxyard._tombstones as tombstones_mod
from boxyard.cmds import new_box, sync_box, delete_box
from boxyard._models import get_boxyard_meta, BoxMeta, SyncCondition, BoxPart
from boxyard._tombstones import list_tombstoned_box_ids
from boxyard._utils.rclone import RcloneFailed

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_tombstone_lookups_are_batched():
    """Tombstones are still honoured, at one remote call instead of N."""
    asyncio.run(_test_tombstone_lookups_are_batched())

# %%
#|set_func_signature
async def _test_tombstone_lookups_are_batched(): ...

# %% [markdown]
# ## Two boxyards, and a box deleted from the second

# %%
#|export
(
    sl_name,
    sl_rclone_path,
    [(config1, config_path1, data_path1), (config2, config_path2, data_path2)],
) = create_boxyards(num_boxyards=2)

live_names = [
    new_box(config_path=config_path1, box_name=f"live-{i}", storage_location=sl_name)
    for i in range(3)
]
doomed_name = new_box(
    config_path=config_path1, box_name="doomed", storage_location=sl_name
)
for name in [*live_names, doomed_name]:
    await sync_box(config_path=config_path1, box_index_name=name)

# Machine 2 deletes it, which writes the tombstone to the shared remote.
from boxyard.cmds import sync_missing_boxmetas, include_box

await sync_missing_boxmetas(config_path=config_path2)
await include_box(config_path=config_path2, box_index_name=doomed_name)
await delete_box(config_path=config_path2, box_index_name=doomed_name)

doomed_id = BoxMeta.extract_box_id(doomed_name)

# %% [markdown]
# ## The bulk fetch returns exactly the deleted box

# %%
#|export
ids = await list_tombstoned_box_ids(config1, sl_name)
assert doomed_id in ids, f"the deleted box is not in {ids}"
assert all(BoxMeta.extract_box_id(n) not in ids for n in live_names), (
    f"a live box was reported as tombstoned: {ids}"
)

# %% [markdown]
# ## Counting the remote calls
#
# This is the whole point of the change, so it is asserted rather than assumed:
# syncing N boxes with the set in hand must not make N probes.

# %%
#|export
probe_calls = []
_real_is_tombstoned = tombstones_mod.is_tombstoned


async def _counting_is_tombstoned(config, storage_location, box_id):
    probe_calls.append(box_id)
    return await _real_is_tombstoned(config, storage_location, box_id)


import boxyard.cmds._sync_box as sync_box_mod

sync_box_mod.is_tombstoned = _counting_is_tombstoned
try:
    # With the set supplied: zero per-box probes.
    for name in live_names:
        await sync_box(
            config_path=config_path1, box_index_name=name, tombstoned_box_ids=ids
        )
    assert probe_calls == [], (
        f"{len(probe_calls)} per-box probes were made despite the set being supplied"
    )

    # Without it: the standalone path still probes, one per box. It must NOT
    # silently skip the check just because no set was passed.
    for name in live_names:
        await sync_box(config_path=config_path1, box_index_name=name)
    assert len(probe_calls) == len(live_names), (
        f"expected one probe per box on the standalone path, got {len(probe_calls)}"
    )
finally:
    sync_box_mod.is_tombstoned = _real_is_tombstoned

# %% [markdown]
# ## A tombstoned box is still refused, by both paths

# %%
#|export
res = await sync_box(
    config_path=config_path1, box_index_name=doomed_name, tombstoned_box_ids=ids
)
assert res[BoxPart.DATA][0].sync_condition == SyncCondition.TOMBSTONED, (
    f"batched path did not refuse a tombstoned box: {res[BoxPart.DATA][0]}"
)

res = await sync_box(config_path=config_path1, box_index_name=doomed_name)
assert res[BoxPart.DATA][0].sync_condition == SyncCondition.TOMBSTONED, (
    f"standalone path did not refuse a tombstoned box: {res[BoxPart.DATA][0]}"
)

# %% [markdown]
# ## A failed lookup must raise, never read as "not tombstoned"
#
# The dangerous failure is the quiet one: if an unreachable remote produced an
# empty set, every box would look untombstoned and a box another machine
# deleted would be resurrected on the next push.

# %%
#|export
# A storage location naming an rclone remote that is not in the rclone config:
# rclone exits 1 ("didn't find section in config file"), which is a genuine
# failure, NOT the exit 3 that means "the directory isn't there yet".
from boxyard.config import StorageConfig

_broken = config1.model_copy(deep=True)
_broken.storage_locations["ghost-remote"] = StorageConfig(
    storage_type=config1.storage_locations[sl_name].storage_type,
    store_path=config1.storage_locations[sl_name].store_path,
)

with pytest.raises(RcloneFailed):
    await list_tombstoned_box_ids(_broken, "ghost-remote")

# And the distinction that makes the empty set safe: a MISSING tombstones
# directory really does mean nothing has been deleted, and yields empty
# rather than raising.
_no_tombstones = config1.model_copy(deep=True)
_no_tombstones.storage_locations[sl_name] = StorageConfig(
    storage_type=config1.storage_locations[sl_name].storage_type,
    store_path=config1.storage_locations[sl_name].store_path / "no-such-subdir",
)
assert await list_tombstoned_box_ids(_no_tombstones, sl_name) == set()

# %%
print("tombstone batching OK")

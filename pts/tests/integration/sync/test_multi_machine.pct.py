# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Multi-Machine Sync Integration Tests
#
# Tests for syncing boxes across multiple machines (simulated with
# multiple local boxyards sharing the same remote storage).
#
# Tests:
# - Syncing boxes between two boxyards
# - Conflict detection when both machines have changes

# %%
#|default_exp integration.sync.test_multi_machine
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import pytest
import asyncio

from boxyard.cmds import (
    new_box,
    sync_box,
    sync_missing_boxmetas,
    include_box,
)
from boxyard._models import get_boxyard_meta, BoxPart
from boxyard._utils.sync_helper import SyncUnsafe

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_multi_machine_sync():
    """Test syncing between multiple machines with conflict detection."""
    asyncio.run(_test_multi_machine_sync())

# %%
#|set_func_signature
async def _test_multi_machine_sync(): ...

# %% [markdown]
# ## Parameters

# %%
#|export
num_boxes = 5

# %% [markdown]
# ## Initialize two boxyards to simulate syncing between machines

# %%
#|export
(
    sl_name,
    sl_rclone_path,
    [(config1, config_path1, data_path1), (config2, config_path2, data_path2)],
) = create_boxyards(num_boxyards=2)

# %% [markdown]
# ## Create boxes on boxyard 1 and sync them

# %%
#|export
box_index_names = []


async def _task(i):
    box_index_name = new_box(
        config_path=config_path1, box_name=f"test_box_{i}", storage_location=sl_name
    )
    await sync_box(config_path=config_path1, box_index_name=box_index_name)
    box_index_names.append(box_index_name)


await asyncio.gather(*[_task(i) for i in range(num_boxes)])

# %% [markdown]
# ## Sync boxmetas into boxyard 2

# %%
#|export
await sync_missing_boxmetas(config_path=config_path2)

boxyard_meta2 = get_boxyard_meta(config2)
assert len(boxyard_meta2.box_metas) == num_boxes

# %% [markdown]
# ## Verify that boxmeta sync only synced metadata (not data)

# %%
async def _task(box_meta):
    assert box_meta.get_local_sync_record_path(config2, BoxPart.META).exists()
    assert not box_meta.check_included(config2)


await asyncio.gather(*[_task(box_meta) for box_meta in boxyard_meta2.box_metas])

# %% [markdown]
# ## Include boxes into boxyard 2

# %%
#|export
async def _task(box_meta):
    await include_box(
        config_path=config_path2,
        box_index_name=box_meta.index_name,
    )


await asyncio.gather(*[_task(box_meta) for box_meta in boxyard_meta2.box_metas])

# %% [markdown]
# ## Modify boxes in boxyard 2 and sync

# %%
#|export
async def _task(box_meta):
    (box_meta.get_local_part_path(config2, BoxPart.DATA) / "hello.txt").write_text(
        "Hello, world!"
    )
    await sync_box(
        config_path=config_path2,
        box_index_name=box_meta.index_name,
    )


await asyncio.gather(*[_task(box_meta) for box_meta in boxyard_meta2.box_metas])

# %% [markdown]
# ## Sync changes into boxyard 1

# %%
#|export
boxyard_meta1 = get_boxyard_meta(config1)

await asyncio.gather(
    *[
        sync_box(
            config_path=config_path1,
            box_index_name=box_meta.index_name,
        )
        for box_meta in boxyard_meta1.box_metas
    ]
)

# %% [markdown]
# ## Verify that the sync worked

# %%
for box_meta in boxyard_meta1.box_metas:
    assert (
        box_meta.get_local_part_path(config1, BoxPart.DATA) / "hello.txt"
    ).exists()

# %% [markdown]
# ## Create a conflict and test that it raises an exception

# %%
#|export
async def _task(box_meta):
    # Create a change on machine 1 and sync
    (box_meta.get_local_part_path(config1, BoxPart.DATA) / "goodbye.txt").write_text(
        "Goodbye, world!"
    )
    await sync_box(
        config_path=config_path1,
        box_index_name=box_meta.index_name,
    )

    # Try to create a conflicting change on machine 2 and sync - should raise
    with pytest.raises(SyncUnsafe):
        (
            box_meta.get_local_part_path(config2, BoxPart.DATA) / "goodbye.txt"
        ).write_text("I'm sorry, world!")
        await sync_box(
            config_path=config_path2,
            box_index_name=box_meta.index_name,
        )


await asyncio.gather(*[_task(box_meta) for box_meta in boxyard_meta1.box_metas])

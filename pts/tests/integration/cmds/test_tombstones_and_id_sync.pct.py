# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Tombstones and ID-Based Sync Integration Tests
#
# Tests for tombstone creation/detection and ID-based syncing.
#
# Tests:
# - Tombstone is created when deleting a box
# - Sync detects tombstoned boxes and returns TOMBSTONED status
# - ID-based sync works when local and remote names differ
# - Remote index cache is used for efficient lookups

# %%
#|default_exp integration.cmds.test_tombstones_and_id_sync
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import asyncio
import pytest
from pathlib import Path

from boxyard.cmds import new_box, delete_box, sync_box
from boxyard.cmds._rename_box import rename_box, RenameScope
from boxyard._models import get_boxyard_meta, BoxPart, BoxMeta, SyncCondition
from boxyard._tombstones import is_tombstoned, get_tombstone, list_tombstones, remove_tombstone
from boxyard._remote_index import find_remote_box_by_id, load_remote_index_cache
from boxyard.config import get_config

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_tombstones_and_id_sync():
    """Test tombstone creation/detection and ID-based syncing."""
    asyncio.run(_test_tombstones_and_id_sync())

# %%
#|set_func_signature
async def _test_tombstones_and_id_sync(): ...

# %% [markdown]
# ## Initialize boxyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %% [markdown]
# ## Test tombstone creation on delete

# %%
#|export
# Create and sync a box
box1 = new_box(
    config_path=config_path,
    box_name="tombstone-test",
    storage_location=remote_name,
)
await sync_box(config_path=config_path, box_index_name=box1)

config = get_config(config_path)
box_meta1 = get_boxyard_meta(config).by_index_name[box1]
box_id1 = box_meta1.box_id

# Verify box exists on remote
remote_index = await find_remote_box_by_id(config, remote_name, box_id1)
assert remote_index is not None

# Delete the box
await delete_box(config_path=config_path, box_index_name=box1)

# Verify tombstone was created
config = get_config(config_path)
tombstoned = await is_tombstoned(config, remote_name, box_id1)
assert tombstoned, "Tombstone should be created on delete"

# Verify tombstone contains correct info
tombstone = await get_tombstone(config, remote_name, box_id1)
assert tombstone is not None
assert tombstone.box_id == box_id1
assert tombstone.last_known_name == "tombstone-test"

# %% [markdown]
# ## Test list_tombstones

# %%
#|export
# Create another box and delete it to have multiple tombstones
box2 = new_box(
    config_path=config_path,
    box_name="tombstone-test-2",
    storage_location=remote_name,
)
await sync_box(config_path=config_path, box_index_name=box2)

config = get_config(config_path)
box_meta2 = get_boxyard_meta(config).by_index_name[box2]
box_id2 = box_meta2.box_id

await delete_box(config_path=config_path, box_index_name=box2)

# List all tombstones
config = get_config(config_path)
tombstones = await list_tombstones(config, remote_name)
tombstone_ids = {t.box_id for t in tombstones}

assert box_id1 in tombstone_ids
assert box_id2 in tombstone_ids

# %% [markdown]
# ## Test remove_tombstone (un-tombstone)

# %%
#|export
# Remove the second tombstone
await remove_tombstone(config, remote_name, box_id2)

# Verify it's no longer tombstoned
config = get_config(config_path)
tombstoned = await is_tombstoned(config, remote_name, box_id2)
assert not tombstoned, "Tombstone should be removed"

# First one should still be tombstoned
tombstoned1 = await is_tombstoned(config, remote_name, box_id1)
assert tombstoned1, "First tombstone should still exist"

# %% [markdown]
# ## Test ID-based sync when names differ

# %%
#|export
# Create a box
box3 = new_box(
    config_path=config_path,
    box_name="id-sync-test",
    storage_location=remote_name,
)
await sync_box(config_path=config_path, box_index_name=box3)

config = get_config(config_path)
box_meta3 = get_boxyard_meta(config).by_index_name[box3]
box_id3 = box_meta3.box_id

# Rename local only so names differ
renamed_box3 = await rename_box(
    config_path=config_path,
    box_index_name=box3,
    new_name="id-sync-test-renamed",
    scope=RenameScope.LOCAL,
)

# Verify names differ
config = get_config(config_path)
local_meta = get_boxyard_meta(config).by_index_name[renamed_box3]
assert local_meta.name == "id-sync-test-renamed"

remote_index = await find_remote_box_by_id(config, remote_name, box_id3)
_, remote_name_part = BoxMeta.parse_index_name(remote_index)
assert remote_name_part == "id-sync-test"  # Different from local

# Add a file to local data to create something to sync
local_data_path = local_meta.get_local_part_path(config, BoxPart.DATA)
(local_data_path / "new_file.txt").write_text("Test content")

# Sync should work despite name mismatch (using ID-based lookup)
sync_result = await sync_box(config_path=config_path, box_index_name=renamed_box3)

# Verify sync succeeded (not an error)
for part, (status, _synced) in sync_result.items():
    assert status.sync_condition != SyncCondition.ERROR, f"Sync failed for {part}: {status}"

# %% [markdown]
# ## Test sync detects tombstoned boxes

# %%
#|export
# Create a new box that will be "orphaned" (we'll manually create its tombstone)
box4 = new_box(
    config_path=config_path,
    box_name="orphan-test",
    storage_location=remote_name,
)
await sync_box(config_path=config_path, box_index_name=box4)

config = get_config(config_path)
box_meta4 = get_boxyard_meta(config).by_index_name[box4]
box_id4 = box_meta4.box_id

# Manually create a tombstone for this box (simulating deletion by another machine)
from boxyard._tombstones import create_tombstone
await create_tombstone(
    config=config,
    storage_location=remote_name,
    box_id=box_id4,
    last_known_name="orphan-test",
)

# Now try to sync - should return TOMBSTONED status
sync_result = await sync_box(config_path=config_path, box_index_name=box4)

# Verify all parts return TOMBSTONED
for part, (status, _synced) in sync_result.items():
    assert status.sync_condition == SyncCondition.TOMBSTONED, f"Expected TOMBSTONED for {part}, got {status.sync_condition}"

print("Tombstone was detected during sync!")

# %% [markdown]
# ## Test remote index cache is populated

# %%
#|export
# Check that the remote index cache was populated during operations
config = get_config(config_path)
cache = load_remote_index_cache(config, remote_name)

# Our renamed box should have a cache entry
assert box_id3 in cache, "Remote index cache should contain box ID"

print("All tombstone and ID-based sync tests passed!")

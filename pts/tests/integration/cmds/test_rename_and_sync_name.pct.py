# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Rename and Sync Name Integration Tests
#
# Tests for renaming boxes and syncing names between local and remote.
#
# Tests:
# - Renaming a box locally only
# - Renaming a box on remote only
# - Renaming a box on both local and remote
# - Sync name from remote to local
# - Sync name from local to remote

# %%
#|default_exp integration.cmds.test_rename_and_sync_name
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
from boxyard.cmds._sync_name import sync_name, SyncNameDirection
from boxyard._models import get_boxyard_meta, BoxPart, BoxMeta
from boxyard._remote_index import find_remote_box_by_id, load_remote_index_cache
from boxyard.config import get_config

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_rename_and_sync_name():
    """Test renaming boxes and syncing names."""
    asyncio.run(_test_rename_and_sync_name())

# %%
#|set_func_signature
async def _test_rename_and_sync_name(): ...

# %% [markdown]
# ## Initialize boxyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %% [markdown]
# ## Test renaming a box locally only

# %%
#|export
# Create and sync a box first
box1 = new_box(
    config_path=config_path,
    box_name="original-name",
    storage_location=remote_name,
)
await sync_box(config_path=config_path, box_index_name=box1)

config = get_config(config_path)
box_meta1 = get_boxyard_meta(config).by_index_name[box1]
box_id = box_meta1.box_id

# Rename locally only
new_index_name = await rename_box(
    config_path=config_path,
    box_index_name=box1,
    new_name="locally-renamed",
    scope=RenameScope.LOCAL,
)

# Verify local name changed
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config)
assert new_index_name in boxyard_meta.by_index_name
assert box1 not in boxyard_meta.by_index_name

renamed_meta = boxyard_meta.by_index_name[new_index_name]
assert renamed_meta.name == "locally-renamed"
assert renamed_meta.box_id == box_id  # ID should be unchanged

# Verify remote still has old name
remote_index = await find_remote_box_by_id(config, remote_name, box_id)
assert remote_index == box1  # Remote should still have original name

# %% [markdown]
# ## Test renaming a box on remote only

# %%
#|export
# Rename remote only (to match local)
await rename_box(
    config_path=config_path,
    box_index_name=new_index_name,
    new_name="locally-renamed",  # Same as local
    scope=RenameScope.REMOTE,
)

# Verify remote now has new name
config = get_config(config_path)
remote_index = await find_remote_box_by_id(config, remote_name, box_id)
assert "locally-renamed" in remote_index

# %% [markdown]
# ## Test renaming a box on both local and remote

# %%
#|export
# Create another box
box2 = new_box(
    config_path=config_path,
    box_name="both-test",
    storage_location=remote_name,
)
await sync_box(config_path=config_path, box_index_name=box2)

config = get_config(config_path)
box_meta2 = get_boxyard_meta(config).by_index_name[box2]
box_id2 = box_meta2.box_id

# Rename both local and remote
new_index_name2 = await rename_box(
    config_path=config_path,
    box_index_name=box2,
    new_name="renamed-both",
    scope=RenameScope.BOTH,
)

# Verify local changed
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config)
assert new_index_name2 in boxyard_meta.by_index_name
assert box2 not in boxyard_meta.by_index_name

# Verify remote changed
remote_index = await find_remote_box_by_id(config, remote_name, box_id2)
assert remote_index == new_index_name2

# %% [markdown]
# ## Test sync_name from local to remote

# %%
#|export
# Create a box and sync
box3 = new_box(
    config_path=config_path,
    box_name="sync-test-local",
    storage_location=remote_name,
)
await sync_box(config_path=config_path, box_index_name=box3)

config = get_config(config_path)
box_meta3 = get_boxyard_meta(config).by_index_name[box3]
box_id3 = box_meta3.box_id

# Rename local only to create name mismatch
renamed_box3 = await rename_box(
    config_path=config_path,
    box_index_name=box3,
    new_name="sync-test-local-renamed",
    scope=RenameScope.LOCAL,
)

# Verify names differ
config = get_config(config_path)
local_meta = get_boxyard_meta(config).by_index_name[renamed_box3]
remote_index = await find_remote_box_by_id(config, remote_name, box_id3)

assert local_meta.name == "sync-test-local-renamed"
_, remote_name_part = BoxMeta.parse_index_name(remote_index)
assert remote_name_part == "sync-test-local"  # Still old name

# Sync name from local to remote
result = await sync_name(
    config_path=config_path,
    box_index_name=renamed_box3,
    direction=SyncNameDirection.TO_REMOTE,
)

# Verify remote now matches local
config = get_config(config_path)
remote_index = await find_remote_box_by_id(config, remote_name, box_id3)
_, remote_name_after = BoxMeta.parse_index_name(remote_index)
assert remote_name_after == "sync-test-local-renamed"

# %% [markdown]
# ## Test sync_name from remote to local

# %%
#|export
# Create a box and sync
box4 = new_box(
    config_path=config_path,
    box_name="sync-test-remote",
    storage_location=remote_name,
)
await sync_box(config_path=config_path, box_index_name=box4)

config = get_config(config_path)
box_meta4 = get_boxyard_meta(config).by_index_name[box4]
box_id4 = box_meta4.box_id

# Rename remote only to create name mismatch
await rename_box(
    config_path=config_path,
    box_index_name=box4,
    new_name="sync-test-remote-renamed",
    scope=RenameScope.REMOTE,
)

# Verify names differ
config = get_config(config_path)
local_meta = get_boxyard_meta(config).by_index_name[box4]
remote_index = await find_remote_box_by_id(config, remote_name, box_id4)

assert local_meta.name == "sync-test-remote"  # Still old name
_, remote_name_part = BoxMeta.parse_index_name(remote_index)
assert remote_name_part == "sync-test-remote-renamed"

# Sync name from remote to local
result = await sync_name(
    config_path=config_path,
    box_index_name=box4,
    direction=SyncNameDirection.TO_LOCAL,
)

# Verify local now matches remote
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config)
assert result in boxyard_meta.by_index_name
local_meta_after = boxyard_meta.by_index_name[result]
assert local_meta_after.name == "sync-test-remote-renamed"

# %% [markdown]
# ## Test that remote index cache is updated after rename

# %%
#|export
# Check that the remote index cache was updated properly
config = get_config(config_path)
cache = load_remote_index_cache(config, remote_name)

# All our renamed boxes should have updated cache entries
assert box_id in cache
assert "locally-renamed" in cache[box_id]

assert box_id2 in cache
assert "renamed-both" in cache[box_id2]

print("All rename and sync_name tests passed!")

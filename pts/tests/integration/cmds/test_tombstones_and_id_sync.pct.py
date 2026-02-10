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
# - Tombstone is created when deleting a repo
# - Sync detects tombstoned repos and returns TOMBSTONED status
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

from repoyard.cmds import new_repo, delete_repo, sync_repo
from repoyard.cmds._rename_repo import rename_repo, RenameScope
from repoyard._models import get_repoyard_meta, RepoPart, RepoMeta, SyncCondition
from repoyard._tombstones import is_tombstoned, get_tombstone, list_tombstones, remove_tombstone
from repoyard._remote_index import find_remote_repo_by_id, load_remote_index_cache
from repoyard.config import get_config

from tests.integration.conftest import create_repoyards

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
# ## Initialize repoyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %% [markdown]
# ## Test tombstone creation on delete

# %%
#|export
# Create and sync a repo
repo1 = new_repo(
    config_path=config_path,
    repo_name="tombstone-test",
    storage_location=remote_name,
)
await sync_repo(config_path=config_path, repo_index_name=repo1)

config = get_config(config_path)
repo_meta1 = get_repoyard_meta(config).by_index_name[repo1]
repo_id1 = repo_meta1.repo_id

# Verify repo exists on remote
remote_index = await find_remote_repo_by_id(config, remote_name, repo_id1)
assert remote_index is not None

# Delete the repo
await delete_repo(config_path=config_path, repo_index_name=repo1)

# Verify tombstone was created
config = get_config(config_path)
tombstoned = await is_tombstoned(config, remote_name, repo_id1)
assert tombstoned, "Tombstone should be created on delete"

# Verify tombstone contains correct info
tombstone = await get_tombstone(config, remote_name, repo_id1)
assert tombstone is not None
assert tombstone.repo_id == repo_id1
assert tombstone.last_known_name == "tombstone-test"

# %% [markdown]
# ## Test list_tombstones

# %%
#|export
# Create another repo and delete it to have multiple tombstones
repo2 = new_repo(
    config_path=config_path,
    repo_name="tombstone-test-2",
    storage_location=remote_name,
)
await sync_repo(config_path=config_path, repo_index_name=repo2)

config = get_config(config_path)
repo_meta2 = get_repoyard_meta(config).by_index_name[repo2]
repo_id2 = repo_meta2.repo_id

await delete_repo(config_path=config_path, repo_index_name=repo2)

# List all tombstones
config = get_config(config_path)
tombstones = await list_tombstones(config, remote_name)
tombstone_ids = {t.repo_id for t in tombstones}

assert repo_id1 in tombstone_ids
assert repo_id2 in tombstone_ids

# %% [markdown]
# ## Test remove_tombstone (un-tombstone)

# %%
#|export
# Remove the second tombstone
await remove_tombstone(config, remote_name, repo_id2)

# Verify it's no longer tombstoned
config = get_config(config_path)
tombstoned = await is_tombstoned(config, remote_name, repo_id2)
assert not tombstoned, "Tombstone should be removed"

# First one should still be tombstoned
tombstoned1 = await is_tombstoned(config, remote_name, repo_id1)
assert tombstoned1, "First tombstone should still exist"

# %% [markdown]
# ## Test ID-based sync when names differ

# %%
#|export
# Create a repo
repo3 = new_repo(
    config_path=config_path,
    repo_name="id-sync-test",
    storage_location=remote_name,
)
await sync_repo(config_path=config_path, repo_index_name=repo3)

config = get_config(config_path)
repo_meta3 = get_repoyard_meta(config).by_index_name[repo3]
repo_id3 = repo_meta3.repo_id

# Rename local only so names differ
renamed_repo3 = await rename_repo(
    config_path=config_path,
    repo_index_name=repo3,
    new_name="id-sync-test-renamed",
    scope=RenameScope.LOCAL,
)

# Verify names differ
config = get_config(config_path)
local_meta = get_repoyard_meta(config).by_index_name[renamed_repo3]
assert local_meta.name == "id-sync-test-renamed"

remote_index = await find_remote_repo_by_id(config, remote_name, repo_id3)
_, remote_name_part = RepoMeta.parse_index_name(remote_index)
assert remote_name_part == "id-sync-test"  # Different from local

# Add a file to local data to create something to sync
local_data_path = local_meta.get_local_part_path(config, RepoPart.DATA)
(local_data_path / "new_file.txt").write_text("Test content")

# Sync should work despite name mismatch (using ID-based lookup)
sync_result = await sync_repo(config_path=config_path, repo_index_name=renamed_repo3)

# Verify sync succeeded (not an error)
for part, (status, _synced) in sync_result.items():
    assert status.sync_condition != SyncCondition.ERROR, f"Sync failed for {part}: {status}"

# %% [markdown]
# ## Test sync detects tombstoned repos

# %%
#|export
# Create a new repo that will be "orphaned" (we'll manually create its tombstone)
repo4 = new_repo(
    config_path=config_path,
    repo_name="orphan-test",
    storage_location=remote_name,
)
await sync_repo(config_path=config_path, repo_index_name=repo4)

config = get_config(config_path)
repo_meta4 = get_repoyard_meta(config).by_index_name[repo4]
repo_id4 = repo_meta4.repo_id

# Manually create a tombstone for this repo (simulating deletion by another machine)
from repoyard._tombstones import create_tombstone
await create_tombstone(
    config=config,
    storage_location=remote_name,
    repo_id=repo_id4,
    last_known_name="orphan-test",
)

# Now try to sync - should return TOMBSTONED status
sync_result = await sync_repo(config_path=config_path, repo_index_name=repo4)

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

# Our renamed repo should have a cache entry
assert repo_id3 in cache, "Remote index cache should contain repo ID"

print("All tombstone and ID-based sync tests passed!")

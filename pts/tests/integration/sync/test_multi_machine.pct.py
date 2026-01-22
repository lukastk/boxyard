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
# Tests for syncing repositories across multiple machines (simulated with
# multiple local repoyards sharing the same remote storage).
#
# Tests:
# - Syncing repos between two repoyards
# - Conflict detection when both machines have changes

# %%
#|default_exp integration.sync.test_multi_machine
#|export_as_func true

# %%
#|hide
import nblite

nblite.nbl_export()

# %%
#|top_export
import pytest
import asyncio

from repoyard.cmds import (
    new_repo,
    sync_repo,
    sync_missing_repometas,
    include_repo,
)
from repoyard._models import get_repoyard_meta, RepoPart
from repoyard._utils.sync_helper import SyncUnsafe

from tests.integration.conftest import create_repoyards

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
num_repos = 5

# %% [markdown]
# ## Initialize two repoyards to simulate syncing between machines

# %%
#|export
(
    sl_name,
    sl_rclone_path,
    [(config1, config_path1, data_path1), (config2, config_path2, data_path2)],
) = create_repoyards(num_repoyards=2)

# %% [markdown]
# ## Create repos on repoyard 1 and sync them

# %%
#|export
repo_index_names = []


async def _task(i):
    repo_index_name = new_repo(
        config_path=config_path1, repo_name=f"test_repo_{i}", storage_location=sl_name
    )
    await sync_repo(config_path=config_path1, repo_index_name=repo_index_name)
    repo_index_names.append(repo_index_name)


await asyncio.gather(*[_task(i) for i in range(num_repos)])

# %% [markdown]
# ## Sync repometas into repoyard 2

# %%
#|export
await sync_missing_repometas(config_path=config_path2)

repoyard_meta2 = get_repoyard_meta(config2)
assert len(repoyard_meta2.repo_metas) == num_repos

# %% [markdown]
# ## Verify that repometa sync only synced metadata (not data)

# %%
async def _task(repo_meta):
    assert repo_meta.get_local_sync_record_path(config2, RepoPart.META).exists()
    assert not repo_meta.check_included(config2)


await asyncio.gather(*[_task(repo_meta) for repo_meta in repoyard_meta2.repo_metas])

# %% [markdown]
# ## Include repos into repoyard 2

# %%
#|export
async def _task(repo_meta):
    await include_repo(
        config_path=config_path2,
        repo_index_name=repo_meta.index_name,
    )


await asyncio.gather(*[_task(repo_meta) for repo_meta in repoyard_meta2.repo_metas])

# %% [markdown]
# ## Modify repos in repoyard 2 and sync

# %%
#|export
async def _task(repo_meta):
    (repo_meta.get_local_part_path(config2, RepoPart.DATA) / "hello.txt").write_text(
        "Hello, world!"
    )
    await sync_repo(
        config_path=config_path2,
        repo_index_name=repo_meta.index_name,
    )


await asyncio.gather(*[_task(repo_meta) for repo_meta in repoyard_meta2.repo_metas])

# %% [markdown]
# ## Sync changes into repoyard 1

# %%
#|export
repoyard_meta1 = get_repoyard_meta(config1)

await asyncio.gather(
    *[
        sync_repo(
            config_path=config_path1,
            repo_index_name=repo_meta.index_name,
        )
        for repo_meta in repoyard_meta1.repo_metas
    ]
)

# %% [markdown]
# ## Verify that the sync worked

# %%
for repo_meta in repoyard_meta1.repo_metas:
    assert (
        repo_meta.get_local_part_path(config1, RepoPart.DATA) / "hello.txt"
    ).exists()

# %% [markdown]
# ## Create a conflict and test that it raises an exception

# %%
#|export
async def _task(repo_meta):
    # Create a change on machine 1 and sync
    (repo_meta.get_local_part_path(config1, RepoPart.DATA) / "goodbye.txt").write_text(
        "Goodbye, world!"
    )
    await sync_repo(
        config_path=config_path1,
        repo_index_name=repo_meta.index_name,
    )

    # Try to create a conflicting change on machine 2 and sync - should raise
    with pytest.raises(SyncUnsafe):
        (
            repo_meta.get_local_part_path(config2, RepoPart.DATA) / "goodbye.txt"
        ).write_text("I'm sorry, world!")
        await sync_repo(
            config_path=config_path2,
            repo_index_name=repo_meta.index_name,
        )


await asyncio.gather(*[_task(repo_meta) for repo_meta in repoyard_meta1.repo_metas])

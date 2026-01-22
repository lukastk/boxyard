# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Basic Sync Integration Tests
#
# Tests for basic repository sync operations:
# - Creating repos
# - Excluding repos
# - Including repos
# - Deleting repos

# %%
#|default_exp integration.sync.test_basic_sync
#|export_as_func true

# %%
#|hide
import nblite

nblite.nbl_export()

# %%
#|top_export
import asyncio
import pytest

from repoyard.cmds import (
    new_repo,
    exclude_repo,
    include_repo,
    delete_repo,
)
from repoyard._models import get_repoyard_meta

from tests.integration.conftest import create_repoyards

# %%
#|top_export
@pytest.mark.integration
def test_basic_sync():
    """Test basic sync operations: create, exclude, include, delete."""
    asyncio.run(_test_basic_sync())

# %%
#|set_func_signature
async def _test_basic_sync(): ...

# %% [markdown]
# ## Parameters

# %%
#|export
num_test_repos = 5

# %% [markdown]
# ## Initialize repoyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %% [markdown]
# ## Create repos using `new_repo` and sync them

# %%
#|export
repo_index_names = []
for i in range(num_test_repos):
    repo_index_name = new_repo(
        config_path=config_path,
        repo_name=f"test_repo_{i}",
        storage_location=remote_name,
    )
    repo_index_names.append(repo_index_name)

# Verify that the repos are included
repoyard_meta = get_repoyard_meta(config, force_create=True)
for repo_index_name in repo_index_names:
    assert repoyard_meta.by_index_name[repo_index_name].check_included(config)

# %% [markdown]
# ## Exclude all repos using `exclude_repo`

# %%
#|export
await asyncio.gather(
    *[
        exclude_repo(config_path=config_path, repo_index_name=repo_index_name)
        for repo_index_name in repo_index_names
    ]
)

# Verify that the repos have been excluded
repoyard_meta = get_repoyard_meta(config, force_create=True)
for repo_index_name in repo_index_names:
    assert not repoyard_meta.by_index_name[repo_index_name].check_included(config)

# %% [markdown]
# ## Include all repos using `include_repo`

# %%
#|export
await asyncio.gather(
    *[
        include_repo(config_path=config_path, repo_index_name=repo_index_name)
        for repo_index_name in repo_index_names
    ]
)

# Verify that the repos are included
repoyard_meta = get_repoyard_meta(config, force_create=True)
for repo_index_name in repo_index_names:
    assert repoyard_meta.by_index_name[repo_index_name].check_included(config)

# %% [markdown]
# ## Delete all repos using `delete_repo`

# %%
#|export
await asyncio.gather(
    *[
        delete_repo(config_path=config_path, repo_index_name=repo_index_name)
        for repo_index_name in repo_index_names
    ]
)

# Verify that the repos have been deleted
for repo_meta in repoyard_meta.by_index_name.values():
    assert not repo_meta.get_local_path(config).exists()
    assert not (remote_rclone_path / repo_meta.get_remote_path(config)).exists()

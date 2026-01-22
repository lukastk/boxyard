# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Include/Exclude Integration Tests
#
# Tests for including and excluding repositories (sparse checkout functionality).
#
# Tests:
# - Excluding a repo removes local data but keeps remote
# - Including a repo downloads data from remote
# - Exclude pushes changes before removing local
# - Include/exclude preserves sync records

# %%
#|default_exp integration.cmds.test_include_exclude
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import asyncio
import pytest
from pathlib import Path

from repoyard.cmds import (
    new_repo,
    sync_repo,
    exclude_repo,
    include_repo,
)
from repoyard._models import get_repoyard_meta, RepoPart
from repoyard.config import get_config

from tests.integration.conftest import create_repoyards

# %%
#|top_export
@pytest.mark.integration
def test_include_exclude():
    """Test include/exclude operations for sparse checkout."""
    asyncio.run(_test_include_exclude())

# %%
#|set_func_signature
async def _test_include_exclude(): ...

# %% [markdown]
# ## Initialize repoyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %% [markdown]
# ## Create a repo with some data

# %%
#|export
repo_index_name = new_repo(
    config_path=config_path,
    repo_name="test-repo",
    storage_location=remote_name,
)

# Refresh config and get repo meta
config = get_config(config_path)
repoyard_meta = get_repoyard_meta(config, force_create=True)
repo_meta = repoyard_meta.by_index_name[repo_index_name]

# Add some test data
data_path = repo_meta.get_local_part_path(config, RepoPart.DATA)
test_file = data_path / "test_data.txt"
test_file.write_text("Hello, World!")

# Sync to remote
await sync_repo(config_path=config_path, repo_index_name=repo_index_name)

# %% [markdown]
# ## Verify repo is included and data exists

# %%
#|export
assert repo_meta.check_included(config)
assert test_file.exists()
assert test_file.read_text() == "Hello, World!"

# %% [markdown]
# ## Exclude the repo

# %%
#|export
await exclude_repo(config_path=config_path, repo_index_name=repo_index_name)

# Refresh config
config = get_config(config_path)
repoyard_meta = get_repoyard_meta(config, force_create=True)
repo_meta = repoyard_meta.by_index_name[repo_index_name]

# %% [markdown]
# ## Verify repo is excluded - local data removed

# %%
#|export
assert not repo_meta.check_included(config)
assert not test_file.exists()

# Verify remote still has data
from repoyard._utils import rclone_lsjson

remote_files = await rclone_lsjson(
    config.rclone_config_path,
    source=remote_name,
    source_path=str(repo_meta.get_remote_path(config)) + "/data",
)
assert remote_files is not None
assert len(remote_files) > 0

# %% [markdown]
# ## Include the repo again

# %%
#|export
await include_repo(config_path=config_path, repo_index_name=repo_index_name)

# Refresh config
config = get_config(config_path)
repoyard_meta = get_repoyard_meta(config, force_create=True)
repo_meta = repoyard_meta.by_index_name[repo_index_name]

# %% [markdown]
# ## Verify repo is included and data restored

# %%
#|export
assert repo_meta.check_included(config)

# Data should be restored
data_path = repo_meta.get_local_part_path(config, RepoPart.DATA)
test_file = data_path / "test_data.txt"
assert test_file.exists()
assert test_file.read_text() == "Hello, World!"

# %% [markdown]
# ## Test exclude pushes local changes first

# %%
#|export
# Make a local change
test_file.write_text("Modified content!")

# Exclude should push changes before removing
await exclude_repo(config_path=config_path, repo_index_name=repo_index_name)

# Include again to verify changes were pushed
await include_repo(config_path=config_path, repo_index_name=repo_index_name)

# Verify the modified content is there
config = get_config(config_path)
repoyard_meta = get_repoyard_meta(config, force_create=True)
repo_meta = repoyard_meta.by_index_name[repo_index_name]
data_path = repo_meta.get_local_part_path(config, RepoPart.DATA)
test_file = data_path / "test_data.txt"

assert test_file.read_text() == "Modified content!"

# %% [markdown]
# ## Cleanup

# %%
#|export
from repoyard.cmds import delete_repo

await delete_repo(config_path=config_path, repo_index_name=repo_index_name)

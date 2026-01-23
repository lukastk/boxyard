# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _include_repo

# %%
#|default_exp cmds._include_repo
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path
import asyncio

from repoyard.config import get_config
from repoyard._utils.locking import RepoyardLockManager, LockAcquisitionError, REPO_SYNC_LOCK_TIMEOUT
from filelock import Timeout

# %%
#|set_func_signature
async def include_repo(
    config_path: Path,
    repo_index_name: str,
    soft_interruption_enabled: bool = True,
):
    """ """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.utils import *

remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %%
# Args
from repoyard.cmds import new_repo

config_path = config_path
repo_index_name = new_repo(
    config_path=config_path, repo_name="test_repo", storage_location="my_remote"
)
soft_interruption_enabled = True

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

# %%
from repoyard.cmds import sync_repo, exclude_repo

# Sync the repo
await sync_repo(config_path=config_path, repo_index_name=repo_index_name)
# Remove the repo from the local store to test the inclusion
await exclude_repo(config_path=config_path, repo_index_name=repo_index_name)

# %% [markdown]
# Check if repo is already included

# %%
#|export
from repoyard._models import get_repoyard_meta

repoyard_meta = get_repoyard_meta(config)

if repo_index_name not in repoyard_meta.by_index_name:
    raise ValueError(f"Repo '{repo_index_name}' does not exist.")

repo_meta = repoyard_meta.by_index_name[repo_index_name]

if repo_meta.check_included(config):
    raise ValueError(f"Repo '{repo_index_name}' is already included.")

# %% [markdown]
# Acquire per-repo sync lock

# %%
#|export
_lock_manager = RepoyardLockManager(config.repoyard_data_path)
_lock_path = _lock_manager.repo_sync_lock_path(repo_index_name)
_lock_manager._ensure_lock_dir(_lock_path)
_sync_lock = __import__('filelock').FileLock(_lock_path, timeout=REPO_SYNC_LOCK_TIMEOUT)
_lock_acquired = False
_loop = asyncio.get_running_loop()
try:
    await _loop.run_in_executor(None, _sync_lock.acquire)
    _lock_acquired = True
except Timeout:
    raise LockAcquisitionError(
        f"repo sync ({repo_index_name})",
        _lock_path,
        REPO_SYNC_LOCK_TIMEOUT,
        message=(
            f"Could not acquire sync lock for repo '{repo_index_name}' within {REPO_SYNC_LOCK_TIMEOUT}s. "
            f"Another sync, include, exclude, or delete operation may be in progress on this repo."
        )
    )

# %% [markdown]
# Include it

# %%
#|export
from repoyard.cmds import sync_repo
from repoyard._models import RepoPart
from repoyard._utils.sync_helper import SyncSetting, SyncDirection

# First force sync the data
await sync_repo(
    config_path=config_path,
    repo_index_name=repo_index_name,
    sync_direction=SyncDirection.PULL,
    sync_setting=SyncSetting.FORCE,
    sync_choices=[RepoPart.DATA],
    soft_interruption_enabled=soft_interruption_enabled,
    _skip_lock=True,
)

# Then sync the rest
await sync_repo(
    config_path=config_path,
    repo_index_name=repo_index_name,
    sync_direction=None,
    sync_setting=SyncSetting.CAREFUL,
    sync_choices=[RepoPart.META, RepoPart.CONF],
    soft_interruption_enabled=soft_interruption_enabled,
    _skip_lock=True,
)

# %% [markdown]
# Release the sync lock

# %%
#|export
if _lock_acquired:
    await _loop.run_in_executor(None, _sync_lock.release)

# %%
# Should now be included
assert repo_meta.check_included(config)

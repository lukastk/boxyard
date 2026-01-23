# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _delete_repo

# %%
#|default_exp cmds._delete_repo
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path
import asyncio

from repoyard.config import get_config
from repoyard._utils import enable_soft_interruption
from repoyard._utils.locking import RepoyardLockManager, LockAcquisitionError, REPO_SYNC_LOCK_TIMEOUT
from filelock import Timeout

# %%
#|set_func_signature
async def delete_repo(
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

# %%
from repoyard.cmds import sync_repo

await sync_repo(config_path=config_path, repo_index_name=repo_index_name)

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

if soft_interruption_enabled:
    enable_soft_interruption()

# %% [markdown]
# Ensure that repo exists

# %%
#|export
from repoyard._models import get_repoyard_meta

repoyard_meta = get_repoyard_meta(config)

if repo_index_name not in repoyard_meta.by_index_name:
    raise ValueError(f"Repo '{repo_index_name}' does not exist.")

repo_meta = repoyard_meta.by_index_name[repo_index_name]

# %%
assert repo_meta.get_local_path(config).exists()
assert (remote_rclone_path / repo_meta.get_remote_path(config)).exists()

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
# Delete the repo

# %%
#|export

# Delete local repo
import shutil
from repoyard._models import RepoPart

shutil.rmtree(
    repo_meta.get_local_part_path(config, RepoPart.DATA)
)  # Deleting separately as the data part is in a separate directory
shutil.rmtree(repo_meta.get_local_path(config))

# Delete remote repo
from repoyard._utils import rclone_purge
from repoyard.config import StorageType

if repo_meta.get_storage_location_config(config).storage_type != StorageType.LOCAL:
    await rclone_purge(
        config.rclone_config_path,
        source=repo_meta.storage_location,
        source_path=repo_meta.get_remote_path(config),
    )

# %%
assert not repo_meta.get_local_path(config).exists()
assert not (remote_rclone_path / repo_meta.get_remote_path(config)).exists()

# %% [markdown]
# Refresh the repoyard meta file

# %%
#|export
from repoyard._models import refresh_repoyard_meta

refresh_repoyard_meta(config)

# %% [markdown]
# Release the sync lock

# %%
#|export
if _lock_acquired:
    await _loop.run_in_executor(None, _sync_lock.release)

# %%
from repoyard._models import get_repoyard_meta

repoyard_meta = get_repoyard_meta(config)
assert len(repoyard_meta.by_index_name) == 0

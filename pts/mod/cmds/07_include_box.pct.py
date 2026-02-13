# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _include_box

# %%
#|default_exp cmds._include_box
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path
import asyncio

from boxyard.config import get_config
from boxyard._utils.locking import BoxyardLockManager, LockAcquisitionError, REPO_SYNC_LOCK_TIMEOUT, acquire_lock_async

# %%
#|set_func_signature
async def include_box(
    config_path: Path,
    box_index_name: str,
    soft_interruption_enabled: bool = True,
):
    """ """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_boxyards

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %%
# Args
from boxyard.cmds import new_box

config_path = config_path
box_index_name = new_box(
    config_path=config_path, box_name="test_box", storage_location="my_remote"
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
from boxyard.cmds import sync_box, exclude_box

# Sync the box
await sync_box(config_path=config_path, box_index_name=box_index_name)
# Remove the box from the local store to test the inclusion
await exclude_box(config_path=config_path, box_index_name=box_index_name)

# %% [markdown]
# Check if box is already included

# %%
#|export
from boxyard._models import get_boxyard_meta

boxyard_meta = get_boxyard_meta(config)

if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' does not exist.")

box_meta = boxyard_meta.by_index_name[box_index_name]

if box_meta.check_included(config):
    raise ValueError(f"Box '{box_index_name}' is already included.")

# %% [markdown]
# Acquire per-box sync lock and include the box

# %%
#|export
from boxyard.cmds import sync_box
from boxyard._models import BoxPart
from boxyard._utils.sync_helper import SyncSetting, SyncDirection

_lock_manager = BoxyardLockManager(config.boxyard_data_path)
_lock_path = _lock_manager.box_sync_lock_path(box_index_name)
_lock_manager._ensure_lock_dir(_lock_path)
_sync_lock = __import__('filelock').FileLock(_lock_path, timeout=0)
await acquire_lock_async(
    _sync_lock,
    f"box sync ({box_index_name})",
    _lock_path,
    REPO_SYNC_LOCK_TIMEOUT,
)
try:
    # First force sync the data
    await sync_box(
        config_path=config_path,
        box_index_name=box_index_name,
        sync_direction=SyncDirection.PULL,
        sync_setting=SyncSetting.FORCE,
        sync_choices=[BoxPart.DATA],
        soft_interruption_enabled=soft_interruption_enabled,
        _skip_lock=True,
    )

    # Then sync the rest
    await sync_box(
        config_path=config_path,
        box_index_name=box_index_name,
        sync_direction=None,
        sync_setting=SyncSetting.CAREFUL,
        sync_choices=[BoxPart.META, BoxPart.CONF],
        soft_interruption_enabled=soft_interruption_enabled,
        _skip_lock=True,
    )
finally:
    _sync_lock.release()

# %%
# Should now be included
assert box_meta.check_included(config)

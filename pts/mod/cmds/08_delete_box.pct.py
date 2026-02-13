# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _delete_box

# %%
#|default_exp cmds._delete_box
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path
import asyncio

from boxyard.config import get_config
from boxyard._utils import enable_soft_interruption
from boxyard._utils.locking import BoxyardLockManager, LockAcquisitionError, REPO_SYNC_LOCK_TIMEOUT, acquire_lock_async
from boxyard._tombstones import create_tombstone
from boxyard._remote_index import remove_from_remote_index_cache

# %%
#|set_func_signature
async def delete_box(
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

# %%
from boxyard.cmds import sync_box

await sync_box(config_path=config_path, box_index_name=box_index_name)

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
# Ensure that box exists

# %%
#|export
from boxyard._models import get_boxyard_meta

boxyard_meta = get_boxyard_meta(config)

if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' does not exist.")

box_meta = boxyard_meta.by_index_name[box_index_name]

# %%
assert box_meta.get_local_path(config).exists()
assert (remote_rclone_path / box_meta.get_remote_path(config)).exists()

# %% [markdown]
# Acquire per-box sync lock and delete the box

# %%
#|export
import shutil
from boxyard._models import BoxPart, refresh_boxyard_meta, BoxMeta
from boxyard._utils import rclone_purge
from boxyard.config import StorageType

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
    # Extract box_id for tombstone and cache operations
    box_id = BoxMeta.extract_box_id(box_index_name)
    storage_location = box_meta.storage_location

    # Create tombstone BEFORE deleting remote (so other machines can see it)
    if box_meta.get_storage_location_config(config).storage_type != StorageType.LOCAL:
        await create_tombstone(
            config=config,
            storage_location=storage_location,
            box_id=box_id,
            last_known_name=box_meta.name,
        )

    # Delete local box
    local_box_path = box_meta.get_local_part_path(config, BoxPart.DATA)
    if local_box_path.exists():
        shutil.rmtree(local_box_path)  
    shutil.rmtree(box_meta.get_local_path(config))

    # Delete remote box
    if box_meta.get_storage_location_config(config).storage_type != StorageType.LOCAL:
        await rclone_purge(
            config.rclone_config_path,
            source=storage_location,
            source_path=box_meta.get_remote_path(config),
        )

    # Remove from remote index cache
    remove_from_remote_index_cache(config, storage_location, box_id)

    # Refresh the boxyard meta file
    refresh_boxyard_meta(config)
finally:
    _sync_lock.release()

# %%
assert not box_meta.get_local_path(config).exists()
assert not (remote_rclone_path / box_meta.get_remote_path(config)).exists()

# %%
from boxyard._models import get_boxyard_meta

boxyard_meta = get_boxyard_meta(config)
assert len(boxyard_meta.by_index_name) == 0

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _exclude_box

# %%
#|default_exp cmds._exclude_box
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path
import asyncio

from boxyard._utils.sync_helper import SyncSetting
from boxyard.config import get_config
from boxyard._utils.locking import BoxyardLockManager, LockAcquisitionError, BOX_SYNC_LOCK_TIMEOUT, acquire_lock_async

# %%
#|set_func_signature
async def exclude_box(
    config_path: Path,
    box_index_name: str,
    skip_sync: bool = False,
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
# Args (1/2)
from boxyard.cmds import new_box

config_path = config_path
box_index_name = new_box(
    config_path=config_path, box_name="test_box", storage_location="my_remote"
)
skip_sync = True
soft_interruption_enabled = True

# %%
from boxyard.cmds import sync_box

await sync_box(
    config_path=config_path,
    box_index_name=box_index_name,
    soft_interruption_enabled=soft_interruption_enabled,
)

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

# %% [markdown]
# Ensure that box is included

# %%
#|export
from boxyard._models import get_boxyard_meta

boxyard_meta = get_boxyard_meta(config)

if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' does not exist.")

box_meta = boxyard_meta.by_index_name[box_index_name]

if not box_meta.check_included(config):
    raise ValueError(f"Box '{box_index_name}' is already excluded.")

# %% [markdown]
# Check that the box is not local

# %%
#|export
from boxyard.config import StorageType

if box_meta.get_storage_location_config(config).storage_type == StorageType.LOCAL:
    raise ValueError(
        f"Box '{box_index_name}' in local storage location '{box_meta.storage_location}' cannot be excluded."
    )

# %% [markdown]
# ## Release write ownership first, if this machine holds it
#
# `exclude` reads as local housekeeping — "I do not need this box here any
# more" — but on a box THIS machine owns it would leave `boxmeta.toml` naming a
# machine that no longer has the DATA. NO machine could then push it, and the
# only escape would be `boxyard claim --steal` from somewhere else. A command
# that looks local would have frozen the box fleet-wide, silently. So the
# release happens as part of the same operation.
#
# Release pushes META, which makes `exclude` a network operation. If that push
# cannot happen we REFUSE rather than excluding and leaving a stale owner
# behind: excluding-and-leaving-owned is precisely the state this exists to
# prevent, and doing it anyway "because the network was down" would just create
# it by a different route.
#
# A box owned by ANOTHER machine is unaffected — this machine was never the
# writer, so there is nothing to release.

# %%
#|export
from boxyard._models import BoxMeta

_box_meta_on_disk = BoxMeta.load(config, box_meta.storage_location, box_index_name)

if _box_meta_on_disk.write_owner is not None and (
    _box_meta_on_disk.write_owner == config.machine_name
):
    from boxyard.cmds import release_box

    try:
        await release_box(
            config_path=config_path, box_index_name=box_index_name, verbose=False
        )
    except Exception as e:
        raise RuntimeError(
            f"Cannot exclude '{box_index_name}': this machine is its write "
            f"owner, and giving that up requires pushing the boxmeta, which "
            f"failed ({e}).\n"
            f"Excluding anyway would leave the box owned by a machine that no "
            f"longer has it, which no machine could then push. Run `boxyard "
            f"release -r '{box_index_name}'` once the remote is reachable, then "
            f"exclude."
        ) from e
    print(
        f"Released write ownership of '{box_index_name}' — excluding a box this "
        f"machine owned would otherwise leave it unpushable everywhere."
    )

# %% [markdown]
# Acquire per-box sync lock

# %%
#|export
import shutil
from boxyard._models import BoxPart
from boxyard.cmds import sync_box

_lock_manager = BoxyardLockManager(config.boxyard_data_path)
_lock_path = _lock_manager.box_sync_lock_path(box_index_name)
_lock_manager._ensure_lock_dir(_lock_path)
_sync_lock = __import__('filelock').FileLock(_lock_path, timeout=0)
await acquire_lock_async(
    _sync_lock,
    f"box sync ({box_index_name})",
    _lock_path,
    BOX_SYNC_LOCK_TIMEOUT,
)
try:
    # Sync any changes before removing locally
    if not skip_sync:
        await sync_box(
            config_path=config_path,
            box_index_name=box_index_name,
            sync_setting=SyncSetting.CAREFUL,
            soft_interruption_enabled=soft_interruption_enabled,
            _skip_lock=True,
        )

    # Exclude it - delete local data
    # Retry rmtree to handle macOS race where Finder recreates .DS_Store mid-delete
    import time
    import errno
    _data_path = box_meta.get_local_part_path(config, BoxPart.DATA)
    for _attempt in range(3):
        try:
            shutil.rmtree(_data_path)
            break
        except OSError as e:
            if e.errno == errno.ENOTEMPTY and _attempt < 2:
                time.sleep(0.1)
            else:
                raise
    box_meta.get_local_sync_record_path(config, BoxPart.DATA).unlink()
finally:
    _sync_lock.release()

print(f"Excluded box '{box_meta.name}'")

# %%
# Should now be included
assert not box_meta.check_included(config)

# %% [markdown]
# Test that syncing the box will not automatically include it again

# %%
from boxyard.cmds import sync_box

await sync_box(
    config_path=config_path,
    box_index_name=box_index_name,
)
assert not box_meta.check_included(config)

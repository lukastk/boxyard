# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _get_box_sync_status

# %%
#|default_exp cmds._get_box_sync_status
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path

from boxyard.config import get_config
from boxyard._models import SyncStatus, BoxPart

# %%
#|set_func_signature
async def get_box_sync_status(
    config_path: Path,
    box_index_name: str,
) -> dict[BoxPart, SyncStatus]:
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
    config_path=config_path, box_name="test_box", storage_location=remote_name
)

# %%
# Put an excluded file into the box data folder to make sure it is not synced
(
    data_path / "local_store" / "my_remote" / box_index_name / "test_box" / ".venv"
).mkdir(parents=True, exist_ok=True)
(
    data_path
    / "local_store"
    / "my_remote"
    / box_index_name
    / "test_box"
    / ".venv"
    / "test.txt"
).write_text("test")

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

# %% [markdown]
# Find the box meta

# %%
#|export
from boxyard._models import get_boxyard_meta

boxyard_meta = get_boxyard_meta(config)

if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' not found.")

box_meta = boxyard_meta.by_index_name[box_index_name]

# %% [markdown]
# A `local` storage location has no rclone remote

# %%
#|export
# `remote=box_meta.storage_location` is passed to rclone as a remote NAME, and
# a `local` storage location has no section in `boxyard_rclone.conf` -- so this
# used to fail with `didn't find section in config file`, i.e. `box-status`,
# `yard-status` and `list --status` all blew up on a local-storage box. There
# is nothing to report against: the store IS this machine. Same shape as
# `sync_box`'s branch, so both commands say the same thing.
from boxyard._models import SyncCondition as _SyncCondition, SyncRecord as _SyncRecord
from boxyard._models import SyncStatus as _SyncStatus, BoxPart as _BoxPart
from boxyard.config import StorageType as _StorageType

if box_meta.get_storage_location_config(config).storage_type == _StorageType.LOCAL:
    _local_only_record = _SyncRecord.create(sync_complete=False)
    box_sync_status = {
        box_part: _SyncStatus(
            sync_condition=_SyncCondition.LOCAL_STORAGE,
            local_path_exists=True,
            remote_path_exists=False,
            local_sync_record=_local_only_record,
            remote_sync_record=_local_only_record,
            is_dir=True,
            error_message=None,
        )
        for box_part in _BoxPart
    }
    box_sync_status #|func_return_line

# %%
#|export
from boxyard._models import get_sync_status, BoxPart
import asyncio

# Guard the checkout root BEFORE probing DATA, exactly as `sync_box` does. An
# unmounted volume makes the DATA directory absent, and an absent directory is
# indistinguishable from a deleted tree -- so without this, `box-status` on a
# machine whose removable root is unplugged reports the box as excluded or
# deleted when nothing of the sort is true. `sync_box` RAISES here; a status
# command should still report the other parts, so DATA gets an explicit ERROR
# instead.
from boxyard._checkout import LocalCheckoutState, get_box_checkout_status
from boxyard._models import SyncCondition as _SyncCondition, SyncStatus as _SyncStatus

_checkout_state = get_box_checkout_status(config, box_meta).state
_data_unreadable = _checkout_state in (
    LocalCheckoutState.UNAVAILABLE,
    LocalCheckoutState.RELOCATING,
)
_data_error_status = _SyncStatus(
    sync_condition=_SyncCondition.ERROR,
    local_path_exists=False,
    remote_path_exists=False,
    local_sync_record=None,
    remote_sync_record=None,
    is_dir=True,
    error_message=(
        f"The checkout root for this box is {_checkout_state.value} -- an "
        f"unmounted volume, or an interrupted relocation. DATA cannot be "
        f"judged from a tree that is not there; nothing has been probed."
    ),
)

# Resolve the box's EFFECTIVE exclude file the same way `sync_box` does, so
# `box-status` reports the same modification state a sync would act on. A box's
# own `conf/.rclone_exclude` REPLACES the global default; using the default for
# a box that overrides it could skip a directory the box really does sync.
from boxyard import const as _const

_conf_path = box_meta.get_local_part_path(config, BoxPart.CONF)
_box_exclude_path = _conf_path / _const.RCLONE_EXCLUDE_FILENAME
_effective_exclude_path = (
    _box_exclude_path
    if _box_exclude_path.exists()
    else config.default_rclone_exclude_path
)

_probed_parts = [
    p for p in BoxPart if not (p is BoxPart.DATA and _data_unreadable)
]
tasks = [
    get_sync_status(
        rclone_config_path=config.rclone_config_path,
        local_path=box_meta.get_local_part_path(config, box_part),
        local_sync_record_path=box_meta.get_local_sync_record_path(config, box_part),
        remote=box_meta.storage_location,
        remote_path=box_meta.get_remote_part_path(config, box_part),
        remote_sync_record_path=box_meta.get_remote_sync_record_path(
            config, box_part
        ),
        # Only DATA syncs under an exclude file; META and CONF go through
        # `sync_helper` with none, and their fingerprint baselines are written
        # under `filter_signature(None)`. Passing the DATA exclude for those
        # parts made the signatures mismatch, so `box-status` read them off the
        # mtime fallback while sync read the fingerprint -- and the two could
        # disagree, which is the one thing this command must not do.
        exclude_path=(
            _effective_exclude_path if box_part is BoxPart.DATA else None
        ),
    )
    for box_part in _probed_parts
]

box_sync_status = {
    box_part: sync_status
    for box_part, sync_status in zip(_probed_parts, await asyncio.gather(*tasks))
}
if _data_unreadable:
    box_sync_status[BoxPart.DATA] = _data_error_status

# %%
from boxyard._models import SyncCondition

for box_part in BoxPart:
    assert box_sync_status[box_part].sync_condition == SyncCondition.NEEDS_PUSH

# %%
#|func_return
box_sync_status

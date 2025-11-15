# %% [markdown]
# # sync_helper

# %%
#|default_exp _utils.sync_helper
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path
import subprocess
from typing import Literal
from enum import Enum
import inspect

from repoyard import const

# %%
#|top_export
from repoyard._models import SyncStatus

class SyncSetting(Enum):
    CAREFUL = "careful"
    REPLACE = "replace"
    FORCE = "force"

class SyncDirection(Enum):
    PUSH = "push" # local -> remote
    PULL = "pull" # remote -> local


# %%
#|top_export
class SyncFailed(Exception): pass
class SyncUnsafe(Exception): pass
class InvalidRemotePath(Exception): pass


# %%
#|set_func_signature
async def sync_helper(
    rclone_config_path: str,
    sync_direction: SyncDirection|None, # None = auto
    sync_setting: SyncSetting,
    local_path: str,
    local_sync_record_path: str,
    remote: str,
    remote_path: str,
    remote_sync_record_path: str,
    include_path: Path|None = None,
    exclude_path: Path|None = None,
    filters_path: Path|None = None,
    include: list[str]|None = None,
    exclude: list[str]|None = None,
    filter: list[str]|None = None,
    creator_hostname: str|None = None,
    verbose: bool = False,
    show_rclone_progress: bool = False,
) -> tuple[SyncStatus, bool]:
    """
    Helper to execute the standard routine for syncing a local and remote folder.

    Returns a tuple of the sync status and a boolean indicating if the sync took place.
    """
    ...


# %% [markdown]
# Set up testing args

# %%
# Set up test environment
import tempfile
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = Path(tempfile.mkdtemp(prefix="sync_helper", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
symlink_path = tests_working_dir / "_utils" / "sync_helper"
symlink_path.parent.mkdir(parents=True, exist_ok=True)
if symlink_path.exists() or symlink_path.is_symlink():
    symlink_path.unlink()
symlink_path.symlink_to(test_folder_path, target_is_directory=True) # So that it can be viewed from within the project working directory
data_path = test_folder_path / ".repoyard"

# %%
my_local_path = test_folder_path / "my_local"
my_remote_path = test_folder_path / "my_remote"
my_local_path.mkdir(parents=True, exist_ok=True)

(my_local_path / "file1.txt").write_text("Hello, world!")
(my_local_path / "file2.txt").write_text("Goodbye, world!");
(my_local_path / "a_folder").mkdir(parents=True, exist_ok=True)
(my_local_path / "a_folder" / "file3.txt").write_text("Hello, world!");
(my_local_path / "a_folder" / "file4.txt").write_text("Goodbye, world!");

# %%
(test_folder_path / "rclone.conf").write_text(f"""
[my_remote]
type = alias
remote = {test_folder_path / "my_remote"}
""");

# %%
# Args
rclone_config_path = test_folder_path / "rclone.conf"
sync_direction = None
sync_setting = SyncSetting.CAREFUL
local_path = my_local_path
local_sync_record_path = test_folder_path / "local_syncrecord.rec"
remote = "my_remote"
remote_path = "data"
remote_sync_record_path = "remote_syncrecord.rec"
include_path = None
exclude_path = None
filters_path = None
include = None
exclude = None
filter = None
creator_hostname = None
verbose = True
show_rclone_progress = False

# %% [markdown]
# # Function body

# %%
#|export
if not remote_path:
    raise InvalidRemotePath("Remote path cannot be empty.") # Disqualifying empty remote paths as it can cause issues with the safety mechanisms

# %%
#|export
if sync_direction is None and sync_setting != SyncSetting.CAREFUL:
    raise ValueError("Auto sync direction can only be used with careful sync setting.")

# %%
#|export
from repoyard._models import get_sync_status, SyncCondition

sync_status = await get_sync_status(
    rclone_config_path=rclone_config_path,
    local_path=local_path,
    local_sync_record_path=local_sync_record_path,
    remote=remote,
    remote_path=remote_path,
    remote_sync_record_path=remote_sync_record_path,
)
sync_condition, local_path_exists, remote_path_exists, local_sync_record, remote_sync_record, sync_path_is_dir = sync_status

# %%
assert sync_condition == SyncCondition.NEEDS_PUSH
assert local_path_exists
assert not remote_path_exists
assert local_sync_record is None
assert remote_sync_record is None


# %% [markdown]
# Check sync status

# %%
#|export
def _raise_unsafe():
    raise SyncUnsafe(inspect.cleandoc(f"""
        Sync is unsafe. Info:
            Local exists: {local_path_exists}
            Remote exists: {remote_path_exists}
            Local sync record: {local_sync_record}
            Remote sync record: {remote_sync_record}
            Sync status: {sync_condition.value}
    """))

if sync_setting != SyncSetting.FORCE and sync_condition == SyncCondition.SYNCED:
    if verbose: print("Sync not needed.")
    sync_status, False #|return_line

if sync_direction is None: # auto
    if sync_condition == SyncCondition.NEEDS_PUSH:
        sync_direction = SyncDirection.PUSH
    elif sync_condition == SyncCondition.NEEDS_PULL:
        sync_direction = SyncDirection.PULL
    else:
        _raise_unsafe() # In the case where the sync status is SYNCED, 'auto'-mode should not reach this, as it should have already returned (as auto can only be used in CAREFUL mode)

if sync_setting == SyncSetting.CAREFUL:
    if sync_direction == SyncDirection.PUSH and sync_condition not in [SyncCondition.NEEDS_PUSH, SyncCondition.SYNCED]:
        _raise_unsafe()
    elif sync_direction == SyncDirection.PULL and sync_condition not in [SyncCondition.NEEDS_PULL, SyncCondition.SYNCED]:
        _raise_unsafe()

# %% [markdown]
# Sync

# %%
#|export
from repoyard._utils import rclone_bisync, rclone_sync, BisyncResult, rclone_mkdir, rclone_path_exists
    
def _sync(dry_run: bool, source: str, source_path: str, dest: str, dest_path: str, return_command: bool=False,) -> BisyncResult:
    if not sync_path_is_dir:
        dest_path = Path(dest_path).parent.as_posix() # needed because rlcone sync doesn't seem to accept files on the dest path
        if dest_path == '.': dest_path = ''
    
    if verbose:
        print(f"Syncing {source}:{source_path} to {dest}:{dest_path}.")

    return rclone_sync(
        rclone_config_path=rclone_config_path,
        source=source,
        source_path=source_path,
        dest=dest,
        dest_path=dest_path,
        include=include or [],
        exclude=exclude or [],
        filter=filter or [],
        include_file=include_path,
        exclude_file=exclude_path,
        filters_file=filters_path,
        dry_run=dry_run,
        return_command=return_command,
        verbose=False,
        progress=show_rclone_progress,
    )


# %%
#|export
from repoyard._models import SyncRecord

if sync_direction == SyncDirection.PULL:
    res, stdout, stderr = await _sync(
        dry_run=False,
        source=remote,
        source_path=remote_path,
        dest="",
        dest_path=local_path,
    )
    
    if res:
        # Retrieve the remote sync record and save it locally
        rec = await SyncRecord.rclone_read(rclone_config_path, remote, remote_sync_record_path)
        await rec.rclone_save(rclone_config_path, "", local_sync_record_path)

elif sync_direction == SyncDirection.PUSH:
    res, stdout, stderr = await _sync(
        dry_run=False,
        source="",
        source_path=local_path,
        dest=remote,
        dest_path=remote_path,
    )

    if res:
        # Create a new sync record and save it at the remote
        rec = SyncRecord.create(creator_hostname=creator_hostname)
        await rec.rclone_save(rclone_config_path, "", local_sync_record_path)
        await rec.rclone_save(rclone_config_path, remote, remote_sync_record_path)

else:
    raise ValueError(f"Unknown sync direction: {sync_direction}")

if not res:
    raise SyncFailed(f"Sync failed. Rclone output:\n{stdout}\n{stderr}")

# %% [markdown]
# Check that the sync worked

# %%
from repoyard._utils import rclone_lsjson

_lsjson = await rclone_lsjson(
    rclone_config_path=rclone_config_path,
    source=remote,
    source_path=remote_path,
)

_names = {f["Name"] for f in _lsjson}
assert "a_folder" in _names
assert "file1.txt" in _names
assert "file2.txt" in _names

# %%
assert SyncRecord.model_validate_json(local_sync_record_path.read_text()).ulid == rec.ulid
assert SyncRecord.model_validate_json((test_folder_path / "my_remote" / remote_sync_record_path).read_text()).ulid == rec.ulid

# %%
#|func_return
sync_status, True

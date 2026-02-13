# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # sync_helper

# %% [markdown]
# Syncing from A to B:
#
# 1. Check the sync records. If the sync records says a sync is ongoing, crash.
# 2. Replace the sync record with a new temporary sync record indicating an ongoing sync.
# 3. Sync from A to B with a backup dir on B.
# 4. If...
#    - ...sync completes, then delete the backup and create a sync record.
#    - ...sync is interrupted. Do nothing.

# %%
#|default_exp _utils.sync_helper
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path
import textwrap
from boxyard._utils import check_interrupted, SoftInterruption
from boxyard._enums import SyncSetting, SyncDirection

from boxyard import const

# %%
#|top_export
from boxyard._models import SyncStatus

# %%
#|top_export
class SyncFailed(Exception):
    pass


class SyncUnsafe(Exception):
    pass


class InvalidRemotePath(Exception):
    pass

# %%
#|set_func_signature
async def sync_helper(
    rclone_config_path: str,
    sync_direction: SyncDirection | None,  # None = auto
    sync_setting: SyncSetting,
    local_path: str,
    local_sync_record_path: str,
    remote: str,
    remote_path: str,
    remote_sync_record_path: str,
    local_sync_backups_path: str,
    remote_sync_backups_path: str,
    include_path: Path | None = None,
    exclude_path: Path | None = None,
    filters_path: Path | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    filter: list[str] | None = None,
    delete_backup: bool = True,
    syncer_hostname: str | None = None,
    verbose: bool = False,
    show_rclone_progress: bool = False,
    allow_missing_source: bool = False,
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
data_path = test_folder_path / ".boxyard"

# %%
my_local_path = test_folder_path / "my_local"
my_remote_path = test_folder_path / "my_remote"
my_local_path.mkdir(parents=True, exist_ok=True)

(my_local_path / "file1.txt").write_text("Hello, world!")
(my_local_path / "file2.txt").write_text("Goodbye, world!")
(my_local_path / "a_folder").mkdir(parents=True, exist_ok=True)
(my_local_path / "a_folder" / "file3.txt").write_text("Hello, world!")
(my_local_path / "a_folder" / "file4.txt").write_text("Goodbye, world!")

# %%
(test_folder_path / "rclone.conf").write_text(f"""
[my_remote]
type = alias
remote = {test_folder_path / "my_remote"}
""")

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
local_sync_backups_path = None  # Should not be needed here
remote_sync_backups_path = "backup"
include_path = None
exclude_path = None
filters_path = None
include = None
exclude = None
filter = None
delete_backup = True
syncer_hostname = None
verbose = True
show_rclone_progress = False
allow_missing_source = False

# %% [markdown]
# # Function body

# %%
#|export
if not remote_path:
    raise InvalidRemotePath(
        "Remote path cannot be empty."
    )  # Disqualifying empty remote paths as it can cause issues with the safety mechanisms

# %%
#|export
if sync_direction is None and sync_setting != SyncSetting.CAREFUL:
    raise ValueError("Auto sync direction can only be used with careful sync setting.")

# %% [markdown]
# Check sync status

# %%
#|export
from boxyard._models import get_sync_status, SyncCondition

sync_status = await get_sync_status(
    rclone_config_path=rclone_config_path,
    local_path=local_path,
    local_sync_record_path=local_sync_record_path,
    remote=remote,
    remote_path=remote_path,
    remote_sync_record_path=remote_sync_record_path,
)
(
    sync_condition,
    local_path_exists,
    remote_path_exists,
    local_sync_record,
    remote_sync_record,
    sync_path_is_dir,
    error_message,
) = sync_status

if sync_condition == SyncCondition.ERROR and sync_setting != SyncSetting.FORCE:
    raise Exception(error_message)

# %%
assert sync_condition == SyncCondition.NEEDS_PUSH
assert local_path_exists
assert not remote_path_exists
assert local_sync_record is None
assert remote_sync_record is None

# %%
#|export
def _can_safely_retry_incomplete(sync_cond, sync_dir, local_rec, remote_rec):
    """Check if this machine can safely retry an incomplete sync.

    For SYNC_FROM_REMOTE_INCOMPLETE (pull was interrupted):
        - Local is incomplete, this machine owns it
        - Safe to retry pull

    For SYNC_TO_REMOTE_INCOMPLETE (push was interrupted):
        - Remote is incomplete
        - Only safe if this machine started it (matching incomplete ULIDs on both sides)
    """
    if sync_cond == SyncCondition.SYNC_FROM_REMOTE_INCOMPLETE:
        # Local is incomplete - this machine owns it
        # Safe to retry pull (or auto-direction which will choose pull)
        return sync_dir in (SyncDirection.PULL, None)

    if sync_cond == SyncCondition.SYNC_TO_REMOTE_INCOMPLETE:
        # Remote is incomplete - only safe if this machine started it
        if (local_rec and remote_rec and
            not local_rec.sync_complete and not remote_rec.sync_complete and
            local_rec.ulid == remote_rec.ulid):
            # Matching incomplete ULIDs = this machine started it
            return sync_dir in (SyncDirection.PUSH, None)

    return False


def _raise_unsafe(message=None):
    if message:
        raise SyncUnsafe(message)
    raise SyncUnsafe(
        textwrap.dedent(f"""
        Sync is unsafe. Info:
            Local exists: {local_path_exists}
            Remote exists: {remote_path_exists}
            Local sync record: {local_sync_record}
            Remote sync record: {remote_sync_record}
            Sync condition: {sync_condition.value}
    """).strip()
    )


if sync_setting != SyncSetting.FORCE and sync_condition == SyncCondition.SYNCED:
    if verbose:
        print("Sync not needed.")
    sync_status, False  #|func_return_line

if sync_direction is None:  # auto
    if sync_condition == SyncCondition.NEEDS_PUSH:
        sync_direction = SyncDirection.PUSH
    elif sync_condition == SyncCondition.NEEDS_PULL:
        sync_direction = SyncDirection.PULL
    elif sync_condition == SyncCondition.EXCLUDED:
        if verbose:
            print("Sync not needed as the box is excluded.")
        sync_status, False  #|func_return_line
    elif sync_condition == SyncCondition.SYNC_FROM_REMOTE_INCOMPLETE:
        # Local is incomplete from interrupted pull - this machine can safely retry pull
        if _can_safely_retry_incomplete(sync_condition, SyncDirection.PULL, local_sync_record, remote_sync_record):
            sync_direction = SyncDirection.PULL
        else:
            _raise_unsafe()
    elif sync_condition == SyncCondition.SYNC_TO_REMOTE_INCOMPLETE:
        # Remote is incomplete from interrupted push
        # Only safe to retry if this machine started it (matching incomplete ULIDs)
        if _can_safely_retry_incomplete(sync_condition, SyncDirection.PUSH, local_sync_record, remote_sync_record):
            sync_direction = SyncDirection.PUSH
        else:
            _raise_unsafe(
                "Remote has an incomplete sync from another machine. "
                "Use --sync-setting force to override, or sync from the original machine."
            )
    else:
        _raise_unsafe()  # In the case where the sync status is SYNCED, 'auto'-mode should not reach this, as it should have already returned (as auto can only be used in CAREFUL mode)

if sync_setting == SyncSetting.CAREFUL:
    if sync_direction == SyncDirection.PUSH:
        if sync_condition in [SyncCondition.NEEDS_PUSH, SyncCondition.SYNCED]:
            pass  # Safe to push
        elif sync_condition == SyncCondition.SYNC_TO_REMOTE_INCOMPLETE:
            # Check if this machine can safely retry the interrupted push
            if not _can_safely_retry_incomplete(sync_condition, sync_direction, local_sync_record, remote_sync_record):
                _raise_unsafe(
                    "Remote has an incomplete sync from another machine. "
                    "Use --sync-setting force to override, or sync from the original machine."
                )
        else:
            _raise_unsafe()
    elif sync_direction == SyncDirection.PULL:
        if sync_condition in [SyncCondition.NEEDS_PULL, SyncCondition.SYNCED]:
            pass  # Safe to pull
        elif sync_condition == SyncCondition.SYNC_FROM_REMOTE_INCOMPLETE:
            # Local is incomplete from interrupted pull - this machine can safely retry
            if not _can_safely_retry_incomplete(sync_condition, sync_direction, local_sync_record, remote_sync_record):
                _raise_unsafe()  # This shouldn't happen, but just in case
        else:
            _raise_unsafe()

# %% [markdown]
# Handle missing source when allowed (for optional parts like CONF)

# %%
#|export
# If source doesn't exist and we allow missing source, return early
if allow_missing_source:
    source_exists = remote_path_exists if sync_direction == SyncDirection.PULL else local_path_exists
    if not source_exists:
        if verbose:
            print(f"Source does not exist and allow_missing_source=True. Skipping sync.")
        sync_status, False  #|func_return_line

# %% [markdown]
# Sync

# %%
#|export
from boxyard._utils import rclone_sync, BisyncResult, rclone_mkdir, rclone_purge


async def _sync(
    dry_run: bool,
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
    backup_remote: str,
    backup_path: str,
    return_command: bool = False,
) -> BisyncResult:
    if not sync_path_is_dir:
        dest_path = (
            Path(dest_path).parent.as_posix()
        )  # needed because rlcone sync doesn't seem to accept files on the dest path
        if dest_path == ".":
            dest_path = ""

    if verbose:
        print(
            f"Syncing {source}:{source_path} to {dest}:{dest_path}.  Backup path: {backup_remote}:{backup_path}"
        )

    # Create backup store directory if it doesn't already exist
    await rclone_mkdir(
        rclone_config_path=rclone_config_path,
        source=backup_remote,
        source_path=backup_path,
    )

    return await rclone_sync(
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
        backup_path=f"{backup_remote}:{backup_path}" if backup_remote else backup_path,
        dry_run=dry_run,
        return_command=return_command,
        verbose=False,
        progress=show_rclone_progress,
    )

# %%
#|export
from boxyard._models import SyncRecord

if check_interrupted():
    raise SoftInterruption()

rec = SyncRecord.create(syncer_hostname=syncer_hostname, sync_complete=False)
backup_name = str(rec.ulid)

if sync_direction == SyncDirection.PULL:
    # Save the sync record on local to signify an ongoing sync
    await rec.rclone_save(rclone_config_path, "", local_sync_record_path)

    backup_remote = ""
    backup_path = Path(local_sync_backups_path) / backup_name

    res, stdout, stderr = await _sync(
        dry_run=False,
        source=remote,
        source_path=remote_path,
        dest="",
        dest_path=local_path,
        backup_remote=backup_remote,
        backup_path=backup_path,
    )

    if res:
        # Retrieve the remote sync record and save it locally
        rec = await SyncRecord.rclone_read(
            rclone_config_path, remote, remote_sync_record_path
        )
        await rec.rclone_save(rclone_config_path, "", local_sync_record_path)

elif sync_direction == SyncDirection.PUSH:
    # Save the incomplete sync record on BOTH local and remote to signify an ongoing sync
    # This creates a "sync session" marker - if interrupted, both sides have the same incomplete ULID,
    # proving this machine owns the interrupted sync and can safely retry
    await rec.rclone_save(rclone_config_path, remote, remote_sync_record_path)
    await rec.rclone_save(rclone_config_path, "", local_sync_record_path)

    backup_remote = remote
    backup_path = Path(remote_sync_backups_path) / backup_name

    res, stdout, stderr = await _sync(
        dry_run=False,
        source="",
        source_path=local_path,
        dest=remote,
        dest_path=remote_path,
        backup_remote=backup_remote,
        backup_path=backup_path,
    )

    if res:
        # Create a new sync record and save it at the remote
        rec = SyncRecord.create(syncer_hostname=syncer_hostname, sync_complete=True)
        await rec.rclone_save(rclone_config_path, "", local_sync_record_path)
        await rec.rclone_save(rclone_config_path, remote, remote_sync_record_path)

else:
    raise ValueError(f"Unknown sync direction: {sync_direction}")

if not res:
    raise SyncFailed(f"Sync failed. Rclone output:\n{stdout}\n{stderr}")

if res and delete_backup:
    await rclone_purge(
        rclone_config_path=rclone_config_path,
        source=backup_remote,
        source_path=backup_path,
    )

# %% [markdown]
# Check that the sync worked

# %%
from boxyard._utils import rclone_lsjson

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
assert (
    SyncRecord.model_validate_json(local_sync_record_path.read_text()).ulid == rec.ulid
)
assert (
    SyncRecord.model_validate_json(
        (test_folder_path / "my_remote" / remote_sync_record_path).read_text()
    ).ulid
    == rec.ulid
)

# %%
#|func_return
sync_status, True

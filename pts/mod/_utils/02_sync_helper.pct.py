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
    local_absence_means_excluded: bool = True,
    filters_path: Path | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    filter: list[str] | None = None,
    delete_backup: bool = True,
    syncer_hostname: str | None = None,
    verbose: bool = False,
    show_rclone_progress: bool = False,
    allow_missing_source: bool = False,
    preserve_exec_perms: bool = False,
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
preserve_exec_perms = False

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
    # The same exclude file the transfer below will use, so the
    # "has anything changed?" question is asked about the same set of
    # files the sync would actually move.
    exclude_path=exclude_path,
    local_absence_means_excluded=local_absence_means_excluded,
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
        dest_exists = local_path_exists if sync_direction == SyncDirection.PULL else remote_path_exists
        if (
            not dest_exists
            and local_sync_record is not None
            and not local_sync_record.sync_complete
        ):
            # Neither side has this part, so an INCOMPLETE record describing an
            # interrupted transfer between two things that do not exist is pure
            # noise -- and because this early return happens BEFORE any record
            # is written, no later sync could ever clear it. `boxyard doctor`
            # then reports `interrupted-sync` for that box forever, which is
            # exactly the cries-wolf failure that makes the tool ignorable.
            #
            # Seen on macbook: obako's `conf` had an incomplete local record
            # from an interrupted pull in Feb 2026, while neither the local nor
            # the remote `conf` directory existed at all. It was unclearable.
            #
            # ONLY the both-absent case is resolved. A missing SOURCE with a
            # PRESENT destination means the part was deleted on the other side
            # -- a real divergence that must not be silently reconciled.
            Path(local_sync_record_path).unlink(missing_ok=True)
            if verbose:
                print(
                    "Cleared a stale incomplete sync record: neither side has "
                    "this part, so there was no interrupted transfer to resume."
                )
        if dest_exists:
            # A missing SOURCE with a PRESENT destination is a real divergence
            # -- the part was deleted on one side -- and this branch does not
            # reconcile it (deleting the surviving copy on inference would be
            # worse). What it must not be is SILENT: this exact shape is how a
            # deleted conf/ leaves a stale exclude list active on the remote
            # for ever, with nothing anywhere saying so. Not gated on
            # `verbose`; a divergence that only speaks when asked is the
            # failure mode the review keeps finding.
            _side = "locally" if sync_direction == SyncDirection.PUSH else "on the remote"
            _survivor = "remote" if sync_direction == SyncDirection.PUSH else "local"
            print(
                f"WARNING: '{local_path}' is missing {_side} while the "
                f"{_survivor} copy still exists. Deletions of a whole part do "
                f"not propagate; the surviving copy stays as it is. Pull to "
                f"restore the missing side, or remove the surviving copy "
                f"deliberately if the deletion was intended."
            )
        elif verbose:
            print(f"Source does not exist and allow_missing_source=True. Skipping sync.")
        sync_status, False  #|func_return_line

# %% [markdown]
# Sync

# %%
#|export
from boxyard._utils import rclone_sync, BisyncResult, rclone_mkdir, rclone_purge
from boxyard._utils.rclone import RcloneFailed
from boxyard._utils.perms import generate_exec_manifest, apply_exec_manifest


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
from datetime import datetime, timezone

from boxyard._models import SyncRecord
from boxyard._utils import check_last_time_modified, literal_exclude_names
from boxyard._fingerprint import filter_signature, tree_fingerprint, write_base

if check_interrupted():
    raise SoftInterruption()

# The machine-local baseline the next status check compares against. Written
# only on a COMPLETED sync, and bound to that sync's ULID, so every crash
# ordering degrades to "no usable baseline" -- which is loud (it forces one
# reconcile) rather than a digest that wrongly says "unchanged".
#
# The fingerprint is computed by the CALLER, at a moment where the tree is
# known to equal what the remote will hold when the records say "complete":
# BEFORE the transfer on a push (the tree state the push is acting on), and
# after the transfer on a pull (the tree the pull produced) -- with the pull
# side refusing to bless a tree that changed while the transfer ran. Computing
# it here, at completion time, was the 0.8.1 bug class: a file written while
# the transfer ran was recorded as synced without ever having been transferred,
# and became invisible to every later check.
def _record_baseline(ulid, *, sig: str, fp: "str | None") -> None:
    if fp is None:
        return
    write_base(
        local_sync_record_path,
        sync_record_ulid=str(ulid),
        fingerprint=fp,
        filter_sig=sig,
    )


rec = SyncRecord.create(syncer_hostname=syncer_hostname, sync_complete=False)
backup_name = str(rec.ulid)

if sync_direction == SyncDirection.PULL:
    # Taken BEFORE the transfer starts. Files the pull writes carry the
    # PUSHER's (older) mtimes, so anything under the box newer than this
    # moment was written locally while the transfer ran -- and is therefore
    # not known to be on the remote. The baseline write below refuses to
    # bless such a tree.
    _pull_started_at = datetime.now(timezone.utc)

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
        # Restore the executable bit that the transport dropped, from the manifest
        # that was just pulled into local_path (additive-only; no-op if absent).
        if preserve_exec_perms and sync_path_is_dir:
            apply_exec_manifest(local_path)

        # Retrieve the remote sync record and save it locally
        rec = await SyncRecord.rclone_read(
            rclone_config_path, remote, remote_sync_record_path
        )
        if rec is None:
            # Reachable if the remote record is deleted between the status
            # probe and here -- another machine running `delete`, say. Without
            # this guard the next line raises `AttributeError: 'NoneType'
            # object has no attribute 'rclone_save'`, which tells the user
            # nothing about what actually happened.
            #
            # The local sync record is deliberately left INCOMPLETE, so the
            # condition is SYNC_FROM_REMOTE_INCOMPLETE next time and this
            # machine can safely retry the pull.
            raise SyncFailed(
                f"Pull succeeded but the remote sync record at "
                f"'{remote_sync_record_path}' has disappeared. The local sync "
                f"record is left incomplete; retry the pull."
            )
        # Baseline BEFORE the record, so a crash between the two leaves no
        # baseline rather than one bound to a record that is not there yet.
        # The tree has already been rewritten by the pull and by
        # `apply_exec_manifest`, so this describes what is actually on disk.
        #
        # -- UNLESS something was written into the box while the pull ran.
        # Such a write is on disk but NOT on the remote; blessing it into the
        # baseline reads SYNCED on the next check, and the pull after that
        # SILENTLY DELETES it (rclone sync removes extraneous local files, and
        # the sync backup that momentarily holds it is purged on success). The
        # old mtime test caught exactly this case -- the racing write's mtime
        # exceeded the adopted record's timestamp -- so blessing it would be a
        # regression, not just a gap. Refusing to write leaves "no usable
        # baseline", i.e. the old test, which keeps the box loud until the
        # write is pushed. A pusher with a fast clock can trip this refusal
        # spuriously; that costs staying on the old test for this box, never
        # a wrong answer.
        _sig = filter_signature(exclude_path)
        _newest = check_last_time_modified(
            local_path, exclude_names=literal_exclude_names(exclude_path)
        )
        if _newest is not None and _newest > _pull_started_at:
            if verbose:
                print(
                    "Not recording a sync baseline: files under the box "
                    "changed while the pull ran; the next sync reconciles them."
                )
        else:
            _record_baseline(
                rec.ulid,
                sig=_sig,
                fp=tree_fingerprint(
                    local_path,
                    literal_exclude_names(exclude_path),
                    filter_sig=_sig,
                ),
            )
        await rec.rclone_save(rclone_config_path, "", local_sync_record_path)

elif sync_direction == SyncDirection.PUSH:
    # Capture the current executable bits into the manifest so they travel with
    # the data (the transport itself can't carry Unix mode over e.g. SFTP). Only
    # rewrites when something changed, so it won't churn an otherwise-clean box.
    if preserve_exec_perms and sync_path_is_dir:
        generate_exec_manifest(local_path)

    # Fingerprint BEFORE the transfer (and after the manifest write above,
    # whose output file is part of the digest): the tree state this push is
    # acting on. A file written while rclone is mid-transfer may or may not
    # reach the remote; fingerprinting at completion would bless it as pushed
    # either way, making it invisible to every later check -- the exact bug
    # 0.8.1 shipped for on the restic side ("erring late loses data
    # silently"). Recording the pre-transfer state instead means a mid-push
    # write mismatches the baseline on the next check and costs at most one
    # reconcile push -- the loud direction.
    _push_sig = filter_signature(exclude_path)
    _push_fp = tree_fingerprint(
        local_path, literal_exclude_names(exclude_path), filter_sig=_push_sig
    )

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
        _record_baseline(rec.ulid, sig=_push_sig, fp=_push_fp)
        await rec.rclone_save(rclone_config_path, "", local_sync_record_path)
        await rec.rclone_save(rclone_config_path, remote, remote_sync_record_path)

else:
    raise ValueError(f"Unknown sync direction: {sync_direction}")

if not res:
    raise SyncFailed(f"Sync failed. Rclone output:\n{stdout}\n{stderr}")

if res and delete_backup:
    # The transfer is done and BOTH sync records already say so, so a failure
    # here is a tidiness problem and nothing more: what is left behind is the
    # copy of the files this sync overwrote, which nothing in boxyard ever
    # reads back. Raising would therefore report a completed, correctly
    # recorded sync as a failed one -- and worse, `multi-sync` would retry the
    # box on every pass and mint a FRESH backup directory each time, so the one
    # thing a raise would reliably do is accelerate the leak it was meant to
    # stop.
    #
    # So it is caught. What must never happen again is it being caught
    # SILENTLY: this is the exact line whose discarded return value grew 1,186
    # orphaned directories and 116.4 GiB on the remote between 2025-11 and
    # 2026-08 without one word of complaint. Hence two things instead of a
    # return value nobody read -- an unconditional warning naming the path (not
    # gated on `verbose`; the point is that it is seen), and the durable
    # signal, `boxyard doctor`'s `orphaned-sync-backups` check, which counts
    # the residue however it got there. The warning catches this run; doctor
    # catches the ones nobody was watching, including leaks from a process that
    # was killed before it ever reached this line.
    try:
        await rclone_purge(
            rclone_config_path=rclone_config_path,
            source=backup_remote,
            source_path=backup_path,
        )
    except RcloneFailed as e:
        _backup_str = (
            f"{backup_remote}:{backup_path}" if backup_remote else str(backup_path)
        )
        print(
            f"WARNING: the sync of '{local_path}' completed, but the backup "
            f"directory it made could not be deleted afterwards: "
            f"'{_backup_str}'.\n"
            f"  The sync itself is fine and fully recorded; the directory is "
            f"leftover storage holding the files this sync overwrote.\n"
            f"  Delete it once you are sure you do not want what is in it. "
            f"`boxyard doctor` reports these under `orphaned-sync-backups`.\n"
            f"  rclone said: {e}"
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

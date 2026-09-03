# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _convert_box
#
# Convert one box's DATA from a plain rclone tree to a per-box restic
# repository. See `_dev/RESTIC-DATA-STORAGE-DESIGN-NOTE.md`.
#
# Nothing converts automatically, in either direction. This command is the only
# thing that changes `BoxMeta.storage_format`, it acts on one box that a person
# named, and it verifies a byte-identical restore before anything is removed.
#
# ## The order of the steps is the safety property
#
# A machine on an older boxyard cannot read a restic-backed box. The gate that
# makes that a loud refusal rather than corruption is the pair of remote objects
# `boxes/<box>/data/` and `sync_records/<box>/data.rec` -- and the ORDER they go
# in decides whether there is a window where an old machine does damage.
#
# The design note proposed purging `data/` first. Measured, that leaves a real
# window: with `data/` gone but `data.rec` still present, `get_sync_status`
# finds matching sync records, reports `NEEDS_PUSH` for a box with local
# changes, and an un-upgraded machine RESURRECTS the plain `data/` beside the
# repository. Both formats then exist and diverge with nothing reporting it.
#
# **So the sync record goes FIRST.** `get_sync_status` opens with
#
#     if remote_path_exists and remote_sync_record is None: -> ERROR
#
# which fires the moment the record is gone, while `data/` is still there. Every
# intermediate state is then a loud refusal on every machine, including this one,
# and there is no window at all.
#
# ## The interruption table
#
# Every state a crash can leave, what each machine sees, and how the next run
# recovers. `L` is this machine's local `data.rec`, which is deliberately NOT
# removed during the conversion -- its presence is what makes states 3 and 4
# refuse loudly instead of looking like a fresh box that wants pushing.
#
# | # | after | remote holds | this machine | an un-upgraded peer | recovery |
# |---|---|---|---|---|---|
# | 0 | nothing | `data/`, `.rec` | plain, syncs | plain, syncs | start again |
# | 1 | repo pushed | + `data.restic/` | plain, syncs | plain, syncs; cannot see the repo | re-push is a cheap no-op (dedup) |
# | 2 | verified | unchanged | as 1 | as 1 | as 1 |
# | 3 | `.rec` deleted | `data/`, repo | **ERROR**, refuses | **ERROR**, refuses | re-run continues |
# | 4 | `data/` purged | repo | **ERROR**, refuses | **ERROR**, refuses | re-run continues |
# | 5 | pointer written | + `data.snapshot` | ERROR until boxmeta | **ERROR**, refuses | re-run completes |
# | 6 | boxmeta saved LOCALLY | repo, pointer, boxmeta still says PLAIN | restic, syncs | reads PLAIN, breaks | re-run publishes it |
# | 7 | boxmeta PUBLISHED | complete | restic, syncs | **ERROR**, refuses | done |
#
# States 3-6 are the interrupted ones. State 7 is the steady state, and an
# un-upgraded peer refuses there -- by design, and the reason conversion must
# wait until the whole fleet is upgraded.
#
# ROW 6 IS THE ONE THIS TABLE ORIGINALLY GOT WRONG, and it cost a canary. It
# used to be the last row and was called "complete", because the boxmeta was
# saved on THIS machine. But the remote's copy still said `plain`, so every
# OTHER machine read the box as plain, took the plain path, and looked for a
# `data/` that had just been purged. The remote contradicted itself: restic
# data, plain metadata. Publishing the boxmeta is therefore part of the
# conversion, not a follow-up command for someone to remember.

# %%
#|default_exp cmds._convert_box
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import fnmatch
import os
from pathlib import Path

from boxyard import const
from boxyard._enums import BoxPart, StorageFormat
from boxyard._models import (
    BoxMeta,
    SyncRecord,
    get_boxyard_meta,
    refresh_boxyard_meta,
)
from boxyard._remote_index import find_remote_box_by_id
from boxyard._restic import (
    ResticRepo,
    clear_state,
    pointer_remote_path,
    read_pointer,
    estimate_stored_bytes,
    init_repo,
    pull,
    push,
    rclone_program_for,
    repo_exists,
    repo_url_for_box,
    resolve_restic_password,
    restic_excludes_from_rclone_file,
    write_pointer,
    write_state,
)
from boxyard._utils import (
    rclone_delete_absent_ok,
    rclone_purge_absent_ok,
    rclone_sync,
)
from boxyard._utils.sync_helper import SyncDirection, SyncSetting, sync_helper
from boxyard._utils.locking import BoxyardLockManager
from boxyard.config import StorageType, get_config


class ConversionRefused(Exception):
    """
    The box is not in a state where conversion is safe.

    Always raised BEFORE anything is written, so a refusal never leaves a
    half-converted box.
    """


async def _reverse_to_plain(
    *,
    config,
    box_meta,
    box_index_name,
    remote_index_name,
    store,
    repo,
    local_data,
    remote_data,
    remote_rec_path,
    excludes,
    include_path,
    exclude_path,
    filters_path,
    dry_run,
    say,
    did,
    result,
):
    """
    Take a restic-backed box back to a plain rclone tree.

    THE ORDER IS THE OPPOSITE OF THE FORWARD ONE, AND FOR A REASON.

    Forward, the switch is enforced by DESTROYING the old format's sync record
    first, because a machine on an older boxyard cannot read `storage_format` at
    all -- publishing the boxmeta would redirect nobody, so the only way to stop
    those machines acting on the plain tree is to make every read of it an
    error.

    Reverse has the opposite property: `plain` is the format EVERY build
    understands, including one that has never heard of the key (an absent
    `storage_format` reads as plain). So the switch is enforced by PUBLISHING
    the boxmeta, at the moment when BOTH formats are complete on the remote --
    and the old one is removed only afterwards.

    That ordering is not cosmetic. Removing the repository or the pointer while
    the boxmeta still says `restic` reaches the state "declares restic, has
    neither repo nor pointer", which `sync_data_restic` reads as a box that has
    never been pushed: the next machine to sync would INITIALISE A NEW
    REPOSITORY and push its local tree, recreating the format this command is
    trying to leave. Publishing first makes that state unreachable.

    The consequence is a stronger safety property than the forward direction
    has: every intermediate state here is a WORKING state rather than a loud
    refusal, because at every point the boxmeta names a format that is complete.
    """
    _pointer = await read_pointer(
        config.rclone_config_path, box_meta.storage_location, store,
        remote_index_name,
    )
    _remote_pointer_path = pointer_remote_path(store, remote_index_name)
    _remote_repo_path = (
        store
        / const.REMOTE_BOXES_REL_PATH
        / remote_index_name
        / const.BOX_RESTIC_REL_PATH
    )

    # ALREADY SWITCHED? Then this is a resume, not a reversal.
    #
    # The publish is the switch, so once it has happened the boxmeta says
    # `plain` and everything before it is done. A re-run must therefore NOT
    # stop at "already plain" -- rows 3 and 4 of the interruption table are
    # exactly that state with the repository or the pointer still on the
    # remote, and stopping would orphan them silently. Found by the row-4 test.
    if box_meta.storage_format is StorageFormat.PLAIN:
        _leftovers = _pointer is not None or await repo_exists(repo)
        if not _leftovers:
            result["already"] = "plain"
            say(f"Box '{box_index_name}' is already a plain tree. Nothing to do.")
            return result
        result["already"] = "published"
        say(
            f"Box '{box_index_name}' already publishes as plain; removing the "
            f"restic artifacts a previous run left behind."
        )
        if dry_run:
            return result
        await _remove_restic_artifacts(
            config=config,
            box_meta=box_meta,
            box_index_name=box_index_name,
            pointer_path=_remote_pointer_path,
            repo_path=_remote_repo_path,
            did=did,
        )
        return result

    if _pointer is None:
        raise ReversalRefused(
            f"Box '{box_index_name}' declares restic but has no data.snapshot "
            f"pointer, so there is no snapshot to verify the local tree "
            f"against. Finish the conversion with `boxyard convert -r "
            f"'{box_index_name}'` first, then reverse it."
        )
    _snapshot = _pointer["snapshot"]
    result["snapshot_id"] = _snapshot

    say(f"Reverse '{box_index_name}' to a plain tree:")
    say(f"  from snapshot {_snapshot[:8]} in {repo.url}")
    say("  steps, in order:")
    say("    1. verify the local tree restores byte-identically from the snapshot")
    say(f"    2. push it to the remote plain tree {remote_data.as_posix()}")
    say("    3. PUBLISH storage_format = plain in boxmeta.toml")
    say(f"    4. delete the pointer {_remote_pointer_path.as_posix()}")
    say(f"    5. purge the repository {repo.url}")
    say(
        "  the boxmeta goes BEFORE the repository is removed, the opposite of "
        "the forward order: `plain` is the format every build understands, so "
        "publishing it redirects the whole fleet onto a tree that is already "
        "complete. Removing the repo first would leave the box declaring restic "
        "with no repository, which the next machine to sync reads as a box that "
        "has never been pushed -- and it would create a new one."
    )
    if dry_run:
        return result

    # ---- Step 1 -- verify BEFORE anything is written -----------------------
    #
    # The local checkout is what will become the plain tree, so it must be
    # proved to match the snapshot first. Content, mode AND symlink targets --
    # `compare_trees` is the same comparison the forward conversion uses.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="boxyard-reverse-") as _tmp:
        _restored = Path(_tmp) / "restored"
        _restored.mkdir()
        await pull(repo, _restored, target_snapshot=_snapshot, base_snapshot=None)
        # `excludes` is NOT optional, and the forward path shipped without it:
        # the snapshot was written THROUGH the exclude list, so comparing the
        # whole local checkout against it reports every excluded path as a
        # difference. Measured on a real box: 28,060 files, 16,201 false
        # differences, every one a `.venv/` or `__pycache__/` path. Conversion
        # could not have succeeded on any box with a virtualenv in it.
        # Hence the argument has NO default: a caller that forgets it is
        # exactly this bug, and a default would let it return silently.
        _problems = compare_trees(local_data, _restored, excludes)
    if _problems:
        raise ReversalRefused(
            f"The local checkout of '{box_index_name}' is NOT identical to "
            f"snapshot {_snapshot[:8]}, so nothing has been changed. Sync the "
            f"box first. {len(_problems)} difference(s), first few:\n  "
            + "\n  ".join(_problems[:10])
        )
    did(f"verified the local tree matches snapshot {_snapshot[:8]}")

    # ---- Step 2 -- push the plain tree, with its sync record ---------------
    #
    # Through `sync_helper`, not a bare rclone call, so the remote `data/` and
    # `sync_records/<box>/data.rec` are written exactly as an ordinary plain
    # push writes them -- including the exec-bit manifest, which a plain box
    # needs again because sftp drops the mode.
    await sync_helper(
        rclone_config_path=config.rclone_config_path,
        sync_direction=SyncDirection.PUSH,
        sync_setting=SyncSetting.FORCE,
        local_path=local_data,
        local_sync_record_path=box_meta.get_local_sync_record_path(
            config, BoxPart.DATA
        ),
        remote=box_meta.storage_location,
        remote_path=remote_data,
        remote_sync_record_path=remote_rec_path,
        local_sync_backups_path=config.local_sync_backups_path,
        remote_sync_backups_path=store / const.REMOTE_BACKUP_REL_PATH,
        include_path=include_path,
        exclude_path=exclude_path,
        filters_path=filters_path,
        verbose=False,
        preserve_exec_perms=True,
    )
    did("pushed the plain tree and its sync record")

    # ---- Step 3 -- PUBLISH the format. This is the switch. ----------------
    _on_disk = BoxMeta.load(config, box_meta.storage_location, box_index_name)
    _on_disk.storage_format = StorageFormat.PLAIN
    _on_disk.save(config)
    refresh_boxyard_meta(config)
    from boxyard.cmds import sync_box as _sync_box

    try:
        await _sync_box(
            config_path=config.config_path,
            box_index_name=box_index_name,
            sync_choices=[BoxPart.META],
            verbose=False,
            _skip_lock=True,
        )
    except Exception as _publish_error:
        raise ConversionIncomplete(
            f"'{box_index_name}' now has a complete plain tree on the remote, "
            f"but its boxmeta could not be published, so the fleet still reads "
            f"it as restic: {_publish_error}\n"
            f"Re-run `boxyard convert -r '{box_index_name}' --to-plain` when "
            f"the remote is reachable. Nothing has been removed."
        ) from None
    did("published storage_format = plain -- the fleet now reads the plain tree")

    # ---- Steps 4 and 5 -- remove what nothing reads any more ---------------
    await _remove_restic_artifacts(
        config=config,
        box_meta=box_meta,
        box_index_name=box_index_name,
        pointer_path=_remote_pointer_path,
        repo_path=_remote_repo_path,
        did=did,
    )
    say(f"Reversed '{box_index_name}' to a plain tree.")
    return result


async def _remove_restic_artifacts(
    *, config, box_meta, box_index_name, pointer_path, repo_path, did
):
    """
    Steps 4 and 5, shared by the full reversal and by a resume.

    Absent-ok throughout, for the same reason the forward steps are: a reversal
    resumes by re-running from the top and legitimately finds these already
    gone.
    """
    await rclone_delete_absent_ok(
        rclone_config_path=config.rclone_config_path,
        dest=box_meta.storage_location,
        dest_path=pointer_path.as_posix(),
    )
    did("deleted data.snapshot")

    await rclone_purge_absent_ok(
        rclone_config_path=config.rclone_config_path,
        source=box_meta.storage_location,
        source_path=repo_path.as_posix(),
    )
    did("purged the repository")

    # This machine's restic state describes a repository that is gone.
    clear_state(config.boxyard_data_path, box_index_name)
    did("cleared this machine's restic state")


class ReversalRefused(Exception):
    """
    The box is not in a state where reversing the conversion is safe.

    Raised BEFORE anything is written, like `ConversionRefused`.
    """


class ConversionIncomplete(Exception):
    """
    The data is converted but the fleet cannot yet read it correctly.

    The opposite of `ConversionRefused`: this is raised AFTER the work, and the
    box on this machine is fine. It means the boxmeta did not reach the remote,
    so every other machine still reads the box as plain. Re-running `convert`
    finishes it.
    """


# %%
#|top_export
def compare_trees(source: Path, restored: Path, excludes: list[str]) -> list[str]:
    """
    Differences between a box's DATA and a restore of it. Empty means identical.

    Compares CONTENT, MODE and SYMLINK TARGETS, plus the set of paths in both
    directions -- because the claim being verified is "restic carries this", and
    a verification that only checked bytes would not be checking the claim. The
    exec-bit manifest is excluded from the snapshot on purpose, so it is
    excluded here too rather than reported as a difference.

    `excludes` MUST be the same patterns the push was given. The push honours the
    exclude list, so a snapshot legitimately does not contain `.venv/`,
    `node_modules/` or `__pycache__/`; comparing the whole local tree against it
    reports every one of those as "missing from the restore" and refuses a
    conversion that was in fact perfect. That is not hypothetical -- it is why
    the first real box tried here (28,060 files) failed with 16,201 differences,
    all of them excluded paths. Every test in this file passed throughout,
    because each one builds a tree with no excluded content in it.

    No default: a caller that forgets this argument is exactly the bug above, and
    a default would let it come back silently.

    Pruning is applied to the SOURCE side ONLY, deliberately. The restore stays
    strictly compared, so if restic ever DOES carry an excluded path it still
    surfaces -- as "present only in the restore" -- instead of being hidden by a
    filter applied to both sides.
    """
    skip = {const.BOX_PERMS_MANIFEST_REL_PATH}

    # restic matches an unanchored pattern against the BASENAME at any depth,
    # which is what `restic_excludes_from_rclone_file` relies on. So a path is
    # excluded when ANY of its components matches. Literal names go in a set --
    # all 36 of the shipped patterns are literal, and fnmatch per component per
    # pattern is a measurable cost on a box the size of jackfruit.
    literals = {p for p in excludes if not any(ch in p for ch in "*?[")}
    globs = [p for p in excludes if p not in literals]

    def is_excluded(rel: str) -> bool:
        for part in rel.split("/"):
            if part in literals:
                return True
            if any(fnmatch.fnmatch(part, g) for g in globs):
                return True
        return False

    def walk(root: Path, prune: bool) -> dict[str, os.stat_result]:
        out = {}
        for path in root.rglob("*"):
            rel = path.relative_to(root).as_posix()
            if rel in skip:
                continue
            if prune and is_excluded(rel):
                continue
            out[rel] = path.lstat()
        return out

    left, right = walk(source, prune=True), walk(restored, prune=False)
    problems: list[str] = []

    for rel in sorted(set(left) - set(right)):
        problems.append(f"missing from the restore: {rel}")
    for rel in sorted(set(right) - set(left)):
        problems.append(f"present only in the restore: {rel}")

    import stat as stat_mod

    for rel in sorted(set(left) & set(right)):
        a, b = left[rel], right[rel]
        src, dst = source / rel, restored / rel

        if stat_mod.S_IFMT(a.st_mode) != stat_mod.S_IFMT(b.st_mode):
            problems.append(f"type differs: {rel}")
            continue
        if stat_mod.S_ISLNK(a.st_mode):
            if os.readlink(src) != os.readlink(dst):
                problems.append(f"symlink target differs: {rel}")
            continue
        if stat_mod.S_ISDIR(a.st_mode):
            continue
        if a.st_size != b.st_size:
            problems.append(f"size differs: {rel}")
            continue
        if (a.st_mode & 0o7777) != (b.st_mode & 0o7777):
            problems.append(
                f"mode differs: {rel} "
                f"({a.st_mode & 0o777:o} -> {b.st_mode & 0o777:o})"
            )
            continue
        if src.read_bytes() != dst.read_bytes():
            problems.append(f"content differs: {rel}")

    return problems

# %%
#|set_func_signature
async def convert_box(
    config_path: Path,
    box_index_name: str,
    dry_run: bool = False,
    estimate_size: bool = False,
    to_plain: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Convert one box's DATA to a per-box restic repository.

    Args:
        config_path: Path to the boxyard config file.
        box_index_name: Index name of the box to convert.
        dry_run: Report what would happen and change nothing.
        estimate_size: With `dry_run`, also measure what restic would store, by
            ingesting into a LOCAL temporary repository. Reads the whole tree
            and writes nothing to the remote.
        to_plain: Reverse the conversion -- take a restic-backed box back to a
            plain rclone tree. See `_reverse_to_plain` for why its step order is
            the opposite of the forward one.
        verbose: Print each step as it happens.

    Returns:
        A dict describing what was done (or would be done), including the steps
        taken, whether the box was already partly converted, and the snapshot id.
    """
    ...

# %% [markdown]
# # Function body

# %%
#|export
config = get_config(config_path)
_boxyard_meta = get_boxyard_meta(config)

if box_index_name not in _boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' not found.")

box_meta = _boxyard_meta.by_index_name[box_index_name]
_sl_config = box_meta.get_storage_location_config(config)

result = {
    "box_index_name": box_index_name,
    "dry_run": dry_run,
    "steps": [],
    "snapshot_id": None,
    "already": None,
}


def _say(message: str) -> None:
    if verbose:
        print(message)


def _did(step: str) -> None:
    result["steps"].append(step)
    _say(f"  {step}")

# %% [markdown]
# ## Refusals
#
# Everything that can say no does so before anything is written.

# %%
#|export
# A `local` storage location has no remote, so there is nothing to convert and
# nothing the format would buy: the per-file transaction cost that motivates the
# whole design does not exist on a local disk.
if _sl_config.storage_type == StorageType.LOCAL:
    raise ConversionRefused(
        f"Box '{box_index_name}' is in the local storage location "
        f"'{box_meta.storage_location}'. A local store has no remote, so there "
        f"is nothing to convert."
    )

_local_data = box_meta.get_local_part_path(config, BoxPart.DATA)
if not _local_data.is_dir():
    raise ConversionRefused(
        f"Box '{box_index_name}' is not checked out on this machine, so there "
        f"is nothing to verify a restore against. Conversion must be run from a "
        f"machine that holds the box: `boxyard include -r '{box_index_name}'` "
        f"first, or convert it from a machine that already has it."
    )

# Conversion changes the remote layout under a box while a sync may be moving
# files in it. Detected two ways, because they catch different things:
#
#   1. The per-box sync lock, taken non-blocking. That is the same lock
#      `sync_box` holds for the whole of a sync, so this catches a sync that is
#      RUNNING right now -- including one started by the supervisor loop.
#   2. An incomplete sync record on either side. That catches a sync that was
#      INTERRUPTED and never finished, where no process holds the lock but the
#      box's contents are not settled and a "byte-identical" verification would
#      be verifying a torn tree.
_lock_manager = BoxyardLockManager(config.boxyard_data_path)
_lock_path = _lock_manager.box_sync_lock_path(box_index_name)
_lock_manager._ensure_lock_dir(_lock_path)
_convert_lock = __import__("filelock").FileLock(_lock_path, timeout=0)
try:
    _convert_lock.acquire()
except Exception as _busy:
    raise ConversionRefused(
        f"Box '{box_index_name}' is being synced right now (its sync lock at "
        f"'{_lock_path}' is held). Wait for the pass to finish and try again."
    ) from _busy

# %%
#|export
try:
    _local_rec_path = box_meta.get_local_sync_record_path(config, BoxPart.DATA)
    _local_rec = await SyncRecord.rclone_read(
        config.rclone_config_path, "", _local_rec_path
    )
    if _local_rec is not None and not _local_rec.sync_complete:
        raise ConversionRefused(
            f"Box '{box_index_name}' has an interrupted DATA sync on this "
            f"machine (local sync record {_local_rec.ulid} is incomplete). Its "
            f"contents are not settled, so a byte-identical verification would "
            f"be verifying a torn tree. Finish or resolve the sync first -- "
            f"`boxyard sync -r '{box_index_name}'` -- then convert."
        )

    _box_id = BoxMeta.extract_box_id(box_index_name)
    _remote_index_name = (
        await find_remote_box_by_id(config, box_meta.storage_location, _box_id)
        or box_index_name
    )
    _store = _sl_config.store_path
    _remote_data = (
        _store / const.REMOTE_BOXES_REL_PATH / _remote_index_name / const.BOX_DATA_REL_PATH
    )
    _remote_rec_path = (
        _store
        / const.SYNC_RECORDS_REL_PATH
        / _remote_index_name
        / f"{BoxPart.DATA.value}.rec"
    )

    _remote_rec = await SyncRecord.rclone_read(
        config.rclone_config_path, box_meta.storage_location, _remote_rec_path.as_posix()
    )
    if _remote_rec is not None and not _remote_rec.sync_complete:
        raise ConversionRefused(
            f"Box '{box_index_name}' has an interrupted DATA sync on the remote "
            f"(record {_remote_rec.ulid} is incomplete), so another machine may "
            f"be mid-push. Resolve it before converting."
        )

    if not to_plain and box_meta.storage_format is StorageFormat.RESTIC:
        result["already"] = "converted"
        _say(f"Box '{box_index_name}' is already restic-backed. Nothing to do.")
        result  #|func_return_line
finally:
    pass

# %% [markdown]
# ## What the conversion will do
#
# Reported before it is done, so `--dry-run` and the real run describe the same
# plan in the same words.

# %%
#|export
_repo = ResticRepo(
    url=repo_url_for_box(_store, box_meta.storage_location, _remote_index_name),
    password=resolve_restic_password(config),
    cache_dir=config.boxyard_data_path / "restic_cache",
    rclone_program=rclone_program_for(config.rclone_config_path),
)
_repo.cache_dir.mkdir(parents=True, exist_ok=True)

# The SAME exclude list the plain sync applies, translated for restic. Without
# it the first snapshot would carry `.venv/` and `node_modules/` -- everything
# the fleet-wide list removes -- and the verification below would then insist
# they be restored too.
_conf_exclude = (
    box_meta.get_local_part_path(config, BoxPart.CONF) / const.RCLONE_EXCLUDE_FILENAME
)
_excludes = restic_excludes_from_rclone_file(
    _conf_exclude if _conf_exclude.exists() else config.default_rclone_exclude_path
)

# %%
#|export
# The reverse runs here, once the repository, the excludes and the remote paths
# are all bound. Everything above it -- the lock, the interrupted-sync checks,
# the checkout and local-storage refusals -- applies to both directions.
if to_plain:
    # Reversing READS the repository -- to verify the local tree against the
    # snapshot before anything is destroyed -- so it needs the binary and the
    # password exactly as converting does. Checked before anything is written or
    # printed, the same scar as `include`.
    from boxyard._restic import require_restic_available

    require_restic_available(config, f"reverse '{box_index_name}' to a plain tree")
    result["direction"] = "to-plain"
    # try/finally, matching the forward path: an interrupted or refused reversal
    # must not leave the box's sync lock held. A real crash frees it when the
    # process dies, but a refusal returns to a live process and would otherwise
    # wedge the box until restart.
    try:
        result = await _reverse_to_plain(
            config=config,
            box_meta=box_meta,
            box_index_name=box_index_name,
            remote_index_name=_remote_index_name,
            store=_store,
            repo=_repo,
            local_data=_local_data,
            remote_data=_remote_data,
            remote_rec_path=_remote_rec_path,
            excludes=_excludes,
            include_path=None,
            exclude_path=(
                _conf_exclude
                if _conf_exclude.exists()
                else config.default_rclone_exclude_path
            ),
            filters_path=None,
            dry_run=dry_run,
            say=_say,
            did=_did,
            result=result,
        )
    finally:
        _convert_lock.release()
    result  #|func_return_line

_file_count = sum(1 for p in _local_data.rglob("*") if p.is_file())
_byte_count = sum(
    p.lstat().st_size for p in _local_data.rglob("*") if p.is_file() and not p.is_symlink()
)
result["local_files"] = _file_count
result["local_bytes"] = _byte_count

_say(f"Convert '{box_index_name}' to restic:")
_say(f"  local DATA: {_file_count:,} files, {_byte_count / 2**30:.3f} GiB "
     f"(counts excluded paths too; the snapshot stores fewer)")
_say(f"  repository: {_repo.url}")
_say("  steps, in order:")
_say("    1. push the box into the repository")
_say("    2. restore it to a temp dir and compare content, mode and symlinks")
_say(f"    3. delete the remote sync record {_remote_rec_path.as_posix()}")
_say(f"    4. purge the remote plain tree {_remote_data.as_posix()}")
_say("    5. write data.snapshot, then set storage_format in boxmeta.toml")
_say(
    "  the sync record goes before the tree on purpose: from step 3 onward every "
    "machine, upgraded or not, refuses this box loudly rather than acting on it."
)

# %%
#|export
if dry_run:
    if estimate_size:
        # Measured against a LOCAL throwaway repository, so a dry run writes
        # nothing to the remote. It reads the whole tree and chunks it, which is
        # most of the CPU of a real push -- hence opt-in.
        import tempfile

        _say("  measuring what restic would store (local temp repo, no remote writes)...")
        with tempfile.TemporaryDirectory(prefix="boxyard-estimate-") as _tmp:
            _probe = ResticRepo(
                url=str(Path(_tmp) / "repo"),
                password="estimate-only",
                cache_dir=Path(_tmp) / "cache",
            )
            (Path(_tmp) / "repo").mkdir()
            (Path(_tmp) / "cache").mkdir()
            await init_repo(_probe)
            _estimate = await estimate_stored_bytes(
                _probe, _local_data, box_index_name=box_index_name,
                excludes=_excludes,
            )
        result["estimated_stored_bytes"] = _estimate
        _say(
            f"  restic would store {_estimate / 2**30:.3f} GiB "
            f"against {_byte_count / 2**30:.3f} GiB of files "
            f"({_byte_count / max(_estimate, 1):.1f}x)"
        )
        _say(
            "  NOTE the remote filesystem already compresses plain trees ~1.9x, "
            "and restic's output is encrypted and so incompressible, so the "
            "saving in remote QUOTA is smaller than this ratio."
        )
    _convert_lock.release()
    result  #|func_return_line

# %% [markdown]
# ## Step 1 -- push
#
# Resumable by construction: a repository that already exists is reused, and a
# second push of an unchanged tree costs a no-op backup rather than a re-upload.

# %%
#|export
try:
    if await repo_exists(_repo):
        result["already"] = "repo-exists"
        _did("repository already present; re-using it")
    else:
        await init_repo(_repo)
        _did("initialised the repository")

    _push = await push(
        _repo, _local_data, box_index_name=box_index_name, excludes=_excludes
    )
    result["snapshot_id"] = _push.snapshot_id
    result["canonical"] = _push.canonical
    _did(f"pushed snapshot {_push.snapshot_id[:8]}")
    if not _push.canonical:
        _say(
            "  WARNING: this machine could not use the canonical restic root, so "
            "the snapshot records this machine's own path. The box still syncs, "
            "but other machines lose incremental pulls for it. Check /tmp."
        )

    # ---- Step 2 -- verify BEFORE anything is removed ----------------------
    import tempfile

    with tempfile.TemporaryDirectory(prefix="boxyard-verify-") as _tmp:
        _restored = Path(_tmp) / "restored"
        await pull(
            _repo,
            _restored,
            target_snapshot=_push.snapshot_id,
            base_snapshot=None,
        )
        _problems = compare_trees(_local_data, _restored, _excludes)
        # What restic ACTUALLY carried, which is not `_file_count`. That one is a
        # raw local walk and counts the excluded paths too -- on the first real
        # box converted it said 28,060 where 13,825 files had been compared.
        # Reporting the bigger number would claim a verification that did not
        # happen, so the success line reports the restore's own count.
        _verified = sum(1 for _p in _restored.rglob("*") if _p.is_file())
    if _problems:
        raise ConversionRefused(
            f"The restore of '{box_index_name}' is NOT identical to the box, so "
            f"nothing has been removed and the box is untouched. "
            f"{len(_problems)} difference(s), first few:\n  "
            + "\n  ".join(_problems[:10])
        )
    _did(f"verified {_verified:,} files restore byte-identically (content, mode, symlinks)")

    # ---- Step 3 -- the sync record FIRST ----------------------------------
    # ABSENT-OK, and that is the interruption table talking, not laziness. A
    # conversion resumes by re-running from the top, so rows 3, 4 and 5 arrive
    # here with the record ALREADY deleted by the attempt that crashed. Absence
    # is the legitimate expected state on a resume -- the goal is that the
    # record is gone, not that this call is what removed it.
    #
    # Before 0.6.2 this went unnoticed: `rclone_delete` returned an exit code
    # nobody read, so deleting a file that was not there passed silently. Now it
    # raises, which is right, and the resume path has to say out loud that it
    # tolerates absence. Any OTHER failure -- an unreachable remote -- still
    # raises and still stops the conversion.
    await rclone_delete_absent_ok(
        rclone_config_path=config.rclone_config_path,
        dest=box_meta.storage_location,
        dest_path=_remote_rec_path.as_posix(),
    )
    _did("deleted the remote DATA sync record (the box now refuses on every machine)")

    # ---- Step 4 -- the plain tree ----------------------------------------
    # Absent-ok for the same reason as step 3: rows 4 and 5 resume with the
    # plain tree already purged.
    await rclone_purge_absent_ok(
        rclone_config_path=config.rclone_config_path,
        source=box_meta.storage_location,
        source_path=_remote_data.as_posix(),
    )
    _did("purged the remote plain tree")

    # ---- Step 5 -- publish -----------------------------------------------
    await write_pointer(
        config.rclone_config_path,
        box_meta.storage_location,
        _store,
        _remote_index_name,
        _push.snapshot_id,
        _push.source_path,
    )
    _did("wrote data.snapshot")

    write_state(
        config.boxyard_data_path, box_index_name, _push.snapshot_id,
        files=_push.files,
    )
    _did("recorded this machine's restic state")

    _on_disk = BoxMeta.load(config, box_meta.storage_location, box_index_name)
    _on_disk.storage_format = StorageFormat.RESTIC
    _on_disk.save(config)
    _did("set storage_format = restic in boxmeta.toml")

    # `boxyard_meta.json` is what `doctor`, `multi-sync`, `list` and the shell
    # helpers read. Without this the box would still look PLAIN to all of them
    # until something else happened to refresh the cache -- and `doctor` would
    # report a `storage-format-mismatch` that is not real.
    refresh_boxyard_meta(config)
    _did("refreshed the registry cache")

    # ---- Step 6 -- PUBLISH the boxmeta -----------------------------------
    #
    # Not optional, and not a follow-up command. Until the remote's boxmeta
    # says `restic`, the remote CONTRADICTS ITSELF -- restic data, plain
    # metadata -- and every other machine reads the box as plain, takes the
    # plain path, and looks for a `data/` this conversion has just purged.
    #
    # `_skip_lock` because this command already holds the box's sync lock; the
    # same reason `include_box` passes it.
    from boxyard.cmds import sync_box as _sync_box

    try:
        await _sync_box(
            config_path=config_path,
            box_index_name=box_index_name,
            sync_choices=[BoxPart.META],
            verbose=False,
            _skip_lock=True,
        )
    except Exception as _publish_error:
        raise ConversionIncomplete(
            f"'{box_index_name}' is converted on this machine and on the "
            f"remote, but its boxmeta could not be published, so the rest of "
            f"the fleet still reads it as plain and will fail on it: "
            f"{_publish_error}\n"
            f"Re-run `boxyard convert -r '{box_index_name}'` when the remote is "
            f"reachable, or push it directly with "
            f"`boxyard sync -r '{box_index_name}' -c meta`."
        ) from None
    _did("published the boxmeta, so the fleet reads the box as restic")

    _say(f"Converted '{box_index_name}'.")
finally:
    _convert_lock.release()

# %%
#|func_return
result

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
# | 6 | boxmeta saved | complete | restic, syncs | **ERROR**, refuses | done |
#
# States 3-5 are the interrupted ones and all three refuse. State 6 is the
# steady state, and an un-upgraded peer refuses there too -- by design, and the
# reason conversion must wait until the whole fleet is upgraded.

# %%
#|default_exp cmds._convert_box
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
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
    estimate_stored_bytes,
    init_repo,
    pull,
    push,
    rclone_program_for,
    repo_exists,
    repo_url_for_box,
    resolve_restic_password,
    write_pointer,
    write_state,
)
from boxyard._utils import rclone_delete, rclone_purge
from boxyard._utils.locking import BoxyardLockManager
from boxyard.config import StorageType, get_config


class ConversionRefused(Exception):
    """
    The box is not in a state where conversion is safe.

    Always raised BEFORE anything is written, so a refusal never leaves a
    half-converted box.
    """


# %%
#|top_export
def compare_trees(source: Path, restored: Path) -> list[str]:
    """
    Differences between a box's DATA and a restore of it. Empty means identical.

    Compares CONTENT, MODE and SYMLINK TARGETS, plus the set of paths in both
    directions -- because the claim being verified is "restic carries this", and
    a verification that only checked bytes would not be checking the claim. The
    exec-bit manifest is excluded from the snapshot on purpose, so it is
    excluded here too rather than reported as a difference.
    """
    skip = {const.BOX_PERMS_MANIFEST_REL_PATH}

    def walk(root: Path) -> dict[str, os.stat_result]:
        out = {}
        for path in root.rglob("*"):
            rel = path.relative_to(root).as_posix()
            if rel in skip:
                continue
            out[rel] = path.lstat()
        return out

    left, right = walk(source), walk(restored)
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

    if box_meta.storage_format is StorageFormat.RESTIC:
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

_file_count = sum(1 for p in _local_data.rglob("*") if p.is_file())
_byte_count = sum(
    p.lstat().st_size for p in _local_data.rglob("*") if p.is_file() and not p.is_symlink()
)
result["local_files"] = _file_count
result["local_bytes"] = _byte_count

_say(f"Convert '{box_index_name}' to restic:")
_say(f"  local DATA: {_file_count:,} files, {_byte_count / 2**30:.3f} GiB")
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
                _probe, _local_data, box_index_name=box_index_name
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

    _push = await push(_repo, _local_data, box_index_name=box_index_name)
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
        _problems = compare_trees(_local_data, _restored)
    if _problems:
        raise ConversionRefused(
            f"The restore of '{box_index_name}' is NOT identical to the box, so "
            f"nothing has been removed and the box is untouched. "
            f"{len(_problems)} difference(s), first few:\n  "
            + "\n  ".join(_problems[:10])
        )
    _did(f"verified {_file_count:,} files restore byte-identically (content, mode, symlinks)")

    # ---- Step 3 -- the sync record FIRST ----------------------------------
    await rclone_delete(
        rclone_config_path=config.rclone_config_path,
        dest=box_meta.storage_location,
        dest_path=_remote_rec_path.as_posix(),
    )
    _did("deleted the remote DATA sync record (the box now refuses on every machine)")

    # ---- Step 4 -- the plain tree ----------------------------------------
    await rclone_purge(
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

    write_state(config.boxyard_data_path, box_index_name, _push.snapshot_id)
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

    _say(
        f"Converted '{box_index_name}'. Its boxmeta still needs to reach the "
        f"other machines: `boxyard sync -r '{box_index_name}' -c meta`."
    )
finally:
    _convert_lock.release()

# %%
#|func_return
result

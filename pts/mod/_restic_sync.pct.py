# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _restic_sync
#
# The DATA half of a sync, for a box whose `storage_format` is `restic`. The
# plain half is untouched: `sync_box` dispatches on the format and a plain box
# reaches exactly the same `_sync_part(...)` call it always did.
#
# This layer exists so `sync_box` gains one branch rather than a second
# implementation inside it, and so the mapping below has somewhere to live.
#
# ## Every `SyncCondition`, for a restic-backed box
#
# | condition | restic meaning | reachable |
# |---|---|---|
# | `SYNCED` | pointer == local record, and nothing local changed | yes |
# | `NEEDS_PUSH` | pointer == local record, local changes to send | yes |
# | `NEEDS_PULL` | pointer moved, nothing local changed | yes |
# | `CONFLICT` | pointer moved AND local changed; or the pointer moved under a push | yes |
# | `WRITE_DENIED` | a non-owner holding changes it may not push | yes |
# | `EXCLUDED` | the box exists remotely but is not checked out here | yes |
# | `TOMBSTONED` | decided for the whole box before the format matters | yes, unchanged |
# | `LOCAL_STORAGE` | decided for the whole box before the format matters | yes, unchanged |
# | `ERROR` | the repo cannot be opened, or the box declares restic with no pointer | yes |
# | `SYNC_FROM_REMOTE_INCOMPLETE` | a restore was interrupted; the tree is torn | yes -- see below |
# | `SYNC_TO_REMOTE_INCOMPLETE` | **UNREACHABLE** | no |
#
# `SYNC_TO_REMOTE_INCOMPLETE` cannot happen. It exists because a plain push can
# leave a remote directory half-written, which is why both sides carry matching
# incomplete ULIDs to prove which machine may retry. A restic snapshot either
# exists or does not; an interrupted `backup` leaves orphaned packs and no
# snapshot, which is wasted space rather than a corrupt remote, and the pointer
# is written only after the snapshot exists. So the whole matching-ULID
# machinery has nothing left to describe for DATA.
#
# `SYNC_FROM_REMOTE_INCOMPLETE` very much does still happen, and needs a
# mechanism restic does not provide: a restore that is interrupted leaves a tree
# matching neither snapshot, which `local_is_modified` would report as local
# edits and the box would report `CONFLICT` -- a false conflict after every
# interrupted pull. The local state record therefore carries a `pulling_from`
# marker for the duration of a restore. Seeing it means "resume the pull", and
# this machine owns its own local tree, so resuming is always safe.
#
# ## Concurrent pushers, now that the canonical path exists
#
# Two machines pushing the same box used to record different paths, so each saw
# the other's snapshot as unrelated. With the canonical path they record the
# SAME path, and the picture changes:
#
# - both `backup` calls take non-exclusive locks and both succeed;
# - each uses its own recorded parent, so the two snapshots are SIBLINGS, both
#   valid and both in the repo;
# - the pointer is a plain file, so the last writer wins.
#
# The loser's work is still in the repository, but the pointer does not name it,
# and on its next pass that machine's tree matches its own snapshot exactly --
# so it looks UNMODIFIED, reports `NEEDS_PULL`, and its working tree is replaced.
# Recoverable from the repo until retention forgets that snapshot, but SILENT,
# which is worse than the plain backend's behaviour where the same race produces
# a visible conflict.
#
# So the pointer is written with a check: re-read it immediately before writing,
# and if it no longer names the snapshot this push was based on, another machine
# pushed during ours. The pointer is left alone and the box reports `CONFLICT`.
# That is one extra remote read per PUSH -- pushes are rare per box -- and it
# turns a silent overwrite into a loud refusal. It narrows the race rather than
# eliminating it: rclone offers no compare-and-swap, so a sufficiently exact
# collision still resolves last-write-wins, with both snapshots preserved.
#
# **This strengthens the case for `write_owner` without changing the rule.**
# Unowned still means unrestricted -- that is deliberate, and 321 boxes rely on
# it -- but a box two machines actually write is now a box worth claiming, and
# `doctor`'s `unowned-box` check is where that should be said.

# %%
#|default_exp _restic_sync

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
from pathlib import Path
import time as _time

import boxyard.config
from boxyard import const
from boxyard._enums import BoxPart, SyncDirection, SyncSetting
from boxyard._models import BoxMeta, SyncCondition, SyncRecord, SyncStatus
from boxyard._ownership import write_denied_message
from boxyard._restic import (
    PullMode,
    ResticCondition,
    ResticError,
    ResticRepo,
    get_status,
    init_repo,
    mark_pull_started,
    pointer_remote_path,
    pull,
    push,
    rclone_program_for,
    read_pointer,
    count_tracked_files,
    read_state,
    repo_url_for_box,
    tree_modified_since,
    resolve_restic_password,
    repo_exists,
    write_pointer,
    write_state,
)
from boxyard._fingerprint import filter_signature, local_tree_differs
from boxyard._utils import literal_exclude_names
from boxyard._utils.sync_helper import SyncUnsafe

# %%
#|export
def repo_for_box(
    config: boxyard.config.Config,
    box_meta: BoxMeta,
    remote_index_name: str,
) -> ResticRepo:
    """The repository for one box, reached through boxyard's own rclone config."""
    store = box_meta.get_storage_location_config(config).store_path
    cache = config.boxyard_data_path / "restic_cache"
    cache.mkdir(parents=True, exist_ok=True)
    return ResticRepo(
        url=repo_url_for_box(store, box_meta.storage_location, remote_index_name),
        password=resolve_restic_password(config),
        cache_dir=cache,
        rclone_program=rclone_program_for(config.rclone_config_path),
    )


def _status(
    condition: SyncCondition,
    *,
    local_exists: bool,
    remote_exists: bool,
    error: str | None = None,
) -> SyncStatus:
    """
    A `SyncStatus` for a restic box.

    The two sync-record fields are filled with a synthetic incomplete record,
    exactly as the `TOMBSTONED` and `LOCAL_STORAGE` paths already do: they
    describe a plain transfer that has no analogue here, and no caller reads
    them for a restic box. The board reads `sync_condition` and nothing else.
    """
    placeholder = SyncRecord.create(sync_complete=False)
    return SyncStatus(
        sync_condition=condition,
        local_path_exists=local_exists,
        remote_path_exists=remote_exists,
        local_sync_record=placeholder,
        remote_sync_record=placeholder,
        is_dir=True,
        error_message=error,
    )

# %%
#|export
async def sync_data_restic(
    config: boxyard.config.Config,
    box_meta: BoxMeta,
    remote_index_name: str,
    *,
    may_push: bool,
    sync_direction: SyncDirection | None = None,
    sync_setting: SyncSetting = SyncSetting.CAREFUL,
    excludes: list[str] | None = None,
    exclude_names: "set[str] | None" = None,
    checkout_missing: bool = False,
    verbose: bool = False,
) -> tuple[SyncStatus, bool]:
    """
    Sync one restic-backed box's DATA. Returns `(status, a transfer happened)`.

    Mirrors `sync_helper`'s contract so `sync_box` and the `multi-sync` board
    need no special case: same tuple, same `SyncCondition` vocabulary, same
    "raise when a person must decide, return a status when they must not".
    """
    data_path = box_meta.get_local_part_path(config, BoxPart.DATA)
    store = box_meta.get_storage_location_config(config).store_path
    index_name = box_meta.index_name

    def _say(message: str) -> None:
        if verbose:
            print(message)

    repo = repo_for_box(config, box_meta, remote_index_name)

    pointer = await read_pointer(
        config.rclone_config_path, box_meta.storage_location, store, remote_index_name
    )

    # A box that DECLARES restic but has no pointer is a conversion that did not
    # finish, or a boxmeta that arrived ahead of its data. Never "no data yet":
    # `new_box` writes the pointer as part of creating a restic box, so the
    # absence is always a fault, and guessing would mean pushing a plain tree
    # over a repository or the reverse.
    if pointer is None:
        if not await repo_exists(repo):
            # NEITHER a repository NOR a pointer: this box has never been
            # pushed. That is a brand-new restic box on its first sync, and it
            # is the ordinary path once `restic` is the default -- exactly as a
            # new plain box's first sync creates its remote `data/`.
            #
            # Distinguished from an unfinished CONVERSION by the repository:
            # a conversion that stopped after removing the plain tree leaves a
            # repo with no pointer, which is the branch below.
            if not data_path.is_dir():
                return (
                    _status(SyncCondition.SYNCED, local_exists=False,
                            remote_exists=False),
                    False,
                )
            if not may_push:
                if not any(data_path.iterdir()):
                    return (
                        _status(SyncCondition.SYNCED, local_exists=True,
                                remote_exists=False),
                        False,
                    )
                return (
                    _status(
                        SyncCondition.WRITE_DENIED,
                        local_exists=True,
                        remote_exists=False,
                        error=write_denied_message(config, box_meta),
                    ),
                    False,
                )
            if sync_direction == SyncDirection.PULL:
                raise SyncUnsafe(
                    f"Box '{index_name}' has never been pushed, so there is "
                    f"nothing to pull."
                )

            _say(f"Creating the repository for '{index_name}'.")
            await init_repo(repo)
            # Captured BEFORE the push: this becomes `synced_at_unix`, which the
            # change-detection gate reads as "the tree matched the snapshot at this
            # moment". Stamping it AFTER the push claims agreement for the window in
            # which the box was still being read, so anything written during the push
            # is invisible for ever -- the snapshot predates it and the timestamp
            # postdates it. Measured on a live box worked in during its conversion:
            # 81 files stranded exactly that way, and the next sync took no-op time.
            # Erring EARLY costs one comparison; erring late loses data.
            _pre_push_unix = _time.time()
            result = await push(
                repo, data_path, parent=None, excludes=excludes,
                box_index_name=index_name,
            )
            await write_pointer(
                config.rclone_config_path, box_meta.storage_location, store,
                remote_index_name, result.snapshot_id, result.source_path,
            )
            write_state(
                config.boxyard_data_path, index_name, result.snapshot_id,
                now_unix=_pre_push_unix,
                files=result.files,
            )
            _say(f"Pushed '{index_name}' as {result.snapshot_id[:8]}.")
            return (
                _status(SyncCondition.NEEDS_PUSH, local_exists=True,
                        remote_exists=True),
                True,
            )
        return (
            _status(
                SyncCondition.ERROR,
                local_exists=data_path.is_dir(),
                remote_exists=True,
                error=(
                    f"Box '{index_name}' has a restic repository but no "
                    f"data.snapshot pointer, so nothing says which snapshot is "
                    f"current. Re-run `boxyard convert -r '{index_name}'` to "
                    f"finish the conversion."
                ),
            ),
            False,
        )

    remote_snapshot = pointer["snapshot"]

    # Absent locally, present remotely. TWO states share that shape and only the
    # placement record tells them apart -- getting this wrong is what made
    # `boxyard include` report success and create nothing.
    if not data_path.is_dir():
        if not checkout_missing:
            # No placement, or placement EXCLUDED: the box is deliberately not
            # here, and pulling it would undo `boxyard exclude`. The same
            # reading the plain path takes.
            _say(f"Box '{index_name}' is not included on this machine. Skipping DATA.")
            return (
                _status(SyncCondition.EXCLUDED, local_exists=False, remote_exists=True),
                False,
            )

        # ADOPTION -- placement says INCLUDED here and the tree is not there
        # yet (`LocalCheckoutState.MISSING`). `include` writes the placement
        # FIRST, precisely so an interrupted adoption is recoverable, and then
        # asks for this pull.
        #
        # The plain path needs no case for this because rclone creates the
        # destination on the way past; restic has to be told. Without it,
        # `include` fell into the EXCLUDED branch above, reported success,
        # materialised nothing, and left `sync` telling the person to run
        # `include` -- a loop with no exit. It is the whole migration story:
        # convert on one machine, adopt on the next.
        #
        # `mark_pull_started` before the restore, so an adoption interrupted
        # half-way resumes through the interrupted-restore branch above rather
        # than looking like a torn local edit.
        if sync_direction == SyncDirection.PUSH:
            raise SyncUnsafe(
                f"Box '{index_name}' has no local checkout to push -- it is "
                f"being included on this machine. Pull it first."
            )
        _say(
            f"Materialising '{index_name}' from snapshot "
            f"{remote_snapshot[:8]}."
        )
        mark_pull_started(config.boxyard_data_path, index_name, remote_snapshot)
        await pull(
            repo,
            data_path,
            target_snapshot=remote_snapshot,
            base_snapshot=None,
            excludes=excludes,
        )
        write_state(
            config.boxyard_data_path, index_name, remote_snapshot,
            files=await count_tracked_files(
                repo, data_path, excludes=excludes, box_index_name=index_name
            ),
        )
        _say(f"Materialised '{index_name}' at {remote_snapshot[:8]}.")
        return (
            _status(SyncCondition.NEEDS_PULL, local_exists=True, remote_exists=True),
            True,
        )

    state = read_state(config.boxyard_data_path, index_name)
    local_snapshot = (state or {}).get("snapshot")
    synced_at = (state or {}).get("synced_at_unix")
    expected_files = (state or {}).get("files")

    # ---- an interrupted restore ------------------------------------------
    #
    # The tree matches neither snapshot, so change detection would call it a
    # local edit and the box would report CONFLICT for something the next pass
    # can simply finish. This machine owns its own local tree, so resuming is
    # always safe -- the same reason the plain path lets a machine retry its own
    # interrupted PULL.
    interrupted_pull = (state or {}).get("pulling_from")
    if interrupted_pull:
        if sync_direction == SyncDirection.PUSH:
            raise SyncUnsafe(
                f"Box '{index_name}' has an interrupted restore in progress "
                f"(towards {interrupted_pull[:8]}), so its local tree is torn. "
                f"Let the pull finish before pushing."
            )
        _say(f"Resuming an interrupted restore of '{index_name}'.")
        outcome = await pull(
            repo,
            data_path,
            target_snapshot=interrupted_pull,
            base_snapshot=None,  # the tree is torn; a diff against it is meaningless
            excludes=excludes,
        )
        write_state(
            config.boxyard_data_path, index_name, outcome.snapshot_id,
            files=await count_tracked_files(
                repo, data_path, excludes=excludes, box_index_name=index_name
            ),
        )
        return (
            _status(
                SyncCondition.SYNC_FROM_REMOTE_INCOMPLETE,
                local_exists=True,
                remote_exists=True,
            ),
            True,
        )

    # ---- first sight of a converted box ----------------------------------
    #
    # A machine that already holds a PLAIN checkout of a box converted elsewhere
    # has no restic state record. Not an edge case: it is what happens to every
    # other machine holding the box, on the pass after a conversion, and it is
    # the single most common transition of the whole migration.
    #
    # Without this, `local_snapshot` is None, change detection has no parent and
    # reports every file as new, and a replica whose tree is perfectly fine
    # reports CONFLICT.
    #
    # The question is NOT "does this tree differ from the snapshot" -- a replica
    # that is merely BEHIND differs too, and refusing those would refuse almost
    # every machine. The question is "did this machine hold unpushed work when
    # the box was converted", and the PLAIN sync record answers it exactly:
    # `check_last_time_modified` against the record's timestamp is the same test
    # the plain backend applied to this box a moment earlier.
    #
    # This is a second reason the plain `data.rec` is not deleted during
    # conversion. The first was making the interrupted states loud.
    if local_snapshot is None:
        plain_record = await SyncRecord.rclone_read(
            config.rclone_config_path,
            "",
            box_meta.get_local_sync_record_path(config, BoxPart.DATA),
        )
        if plain_record is None:
            raise SyncUnsafe(
                f"Box '{index_name}' was converted to restic elsewhere, and this "
                f"machine has no record of when it last agreed with the remote, "
                f"so there is no way to tell whether this copy holds unpushed "
                f"work. Nothing has been changed. Take the remote's version with "
                f"`boxyard discard-local -r '{index_name}'` (this machine's copy "
                f"is kept under the sync backups directory)."
            )

        # "Did this replica hold unpushed work when the box was converted?" is
        # exactly the question the plain fingerprint baseline answers, when one
        # exists: it digests every change shape, where the mtime test this used
        # to rely on sees two of ten -- an unpushed deletion, rename, chmod or
        # symlink edit read "clean" here and was silently reverted by the full
        # restore that follows. UNKNOWN (no baseline yet) falls back to that
        # old mtime test: exactly the transition rule `get_sync_status` uses,
        # for the same reason -- never worse than before, strictly better once
        # the box has synced on >= 0.8.0.
        _adoption_exc = box_meta.get_effective_exclude_path(config)
        _adoption_differs = local_tree_differs(
            local_path=data_path,
            local_sync_record_path=box_meta.get_local_sync_record_path(
                config, BoxPart.DATA
            ),
            local_sync_record_ulid=plain_record.ulid,
            exclude_names=literal_exclude_names(_adoption_exc),
            filter_sig=filter_signature(_adoption_exc),
        )
        if _adoption_differs is None:
            # TODO(cleanup): drop this fallback with the others -- once `boxyard
            # doctor` reports 0 uncovered fingerprint baselines on every machine
            # (a sync pass alone does NOT achieve this: an already-synced box
            # never writes a baseline), AND the historical backlog has been
            # reviewed deliberately rather than by upgrade.
            _adoption_differs = tree_modified_since(
                data_path, plain_record.timestamp.timestamp()
            )
        if _adoption_differs:
            if not may_push:
                return (
                    _status(
                        SyncCondition.WRITE_DENIED,
                        local_exists=True,
                        remote_exists=True,
                        error=write_denied_message(config, box_meta),
                    ),
                    False,
                )
            raise SyncUnsafe(
                f"Box '{index_name}' was converted to restic elsewhere, and this "
                f"machine's copy has changed since it last synced -- so it holds "
                f"work the conversion never saw. Nothing has been changed. Take "
                f"the remote's version with `boxyard discard-local -r "
                f"'{index_name}'` (this machine's copy is kept under the sync "
                f"backups directory), or copy the work out first. Converting a "
                f"box whose replicas are all in sync avoids this entirely."
            )

        _say(f"Adopting the converted box '{index_name}' on this machine.")
        mark_pull_started(config.boxyard_data_path, index_name, remote_snapshot)
        await pull(
            repo,
            data_path,
            target_snapshot=remote_snapshot,
            base_snapshot=None,
            excludes=excludes,
        )
        write_state(
            config.boxyard_data_path, index_name, remote_snapshot,
            files=await count_tracked_files(
                repo, data_path, excludes=excludes, box_index_name=index_name
            ),
        )
        return _status(SyncCondition.NEEDS_PULL, local_exists=True, remote_exists=True), True

    # ---- the ordinary decision -------------------------------------------
    restic_status = await get_status(
        repo,
        data_path,
        remote_snapshot=remote_snapshot,
        local_snapshot=local_snapshot,
        synced_at_unix=synced_at,
        excludes=excludes,
        box_index_name=index_name,
        expected_files=expected_files,
        # Literal exclude names for the cheap gate. Without them a `.DS_Store`
        # or a write inside `.venv/` makes the gate say "maybe" and the box pays
        # the full check every pass -- correct, but it gates nothing.
        exclude_names=exclude_names,
    )
    condition = restic_status.condition

    if sync_setting == SyncSetting.FORCE and sync_direction == SyncDirection.PUSH:
        condition = ResticCondition.NEEDS_PUSH
    elif sync_setting == SyncSetting.FORCE and sync_direction == SyncDirection.PULL:
        condition = ResticCondition.NEEDS_PULL

    if condition is ResticCondition.SYNCED:
        if restic_status.clean_check_at_unix is not None:
            # The FULL check just proved the tree clean, which means the cheap
            # gate had been opened by something the check then dismissed -- an
            # excluded-name arrival bumping its directory's mtime, typically a
            # `.DS_Store`. Without a fresh stamp the gate stays open FOR EVER:
            # every later pass pays the 2-5s dry-run, and `--skip-unchanged`
            # never skips this box again. Re-stamp with the moment captured
            # BEFORE the check walked the tree (a mid-check write then reopens
            # the gate -- the loud direction), same snapshot, same file count.
            write_state(
                config.boxyard_data_path, index_name, local_snapshot,
                now_unix=restic_status.clean_check_at_unix,
                files=expected_files,
            )
        _say(f"'{index_name}' DATA is up to date ({remote_snapshot[:8]}).")
        return _status(SyncCondition.SYNCED, local_exists=True, remote_exists=True), False

    if condition is ResticCondition.NEEDS_PULL:
        if sync_direction == SyncDirection.PUSH:
            raise SyncUnsafe(
                f"Box '{index_name}' needs a PULL ({remote_snapshot[:8]}), but a "
                f"push was asked for."
            )
        mark_pull_started(config.boxyard_data_path, index_name, remote_snapshot)
        outcome = await pull(
            repo,
            data_path,
            target_snapshot=remote_snapshot,
            base_snapshot=local_snapshot,
            excludes=excludes,
        )
        write_state(
            config.boxyard_data_path, index_name, remote_snapshot,
            files=await count_tracked_files(
                repo, data_path, excludes=excludes, box_index_name=index_name
            ),
        )
        _say(
            f"Pulled '{index_name}' to {remote_snapshot[:8]} "
            f"({outcome.mode.value}, {outcome.changed} changed, "
            f"{outcome.removed} removed)."
        )
        return _status(SyncCondition.NEEDS_PULL, local_exists=True, remote_exists=True), True

    if condition is ResticCondition.NEEDS_PUSH:
        if not may_push:
            # A read-only replica holding changes that will never leave this
            # machine. A CONDITION and not an exception, for the same reason the
            # plain path made it one: `multi-sync` runs every 1200s and a raise
            # here would manufacture the same unresolvable error 72 times a day.
            return (
                _status(
                    SyncCondition.WRITE_DENIED,
                    local_exists=True,
                    remote_exists=True,
                    error=write_denied_message(config, box_meta),
                ),
                False,
            )
        if sync_direction == SyncDirection.PULL:
            raise SyncUnsafe(
                f"Box '{index_name}' has local changes to push, but a pull was "
                f"asked for."
            )

        # Captured BEFORE the push: this becomes `synced_at_unix`, which the
        # change-detection gate reads as "the tree matched the snapshot at this
        # moment". Stamping it AFTER the push claims agreement for the window in
        # which the box was still being read, so anything written during the push
        # is invisible for ever -- the snapshot predates it and the timestamp
        # postdates it. Measured on a live box worked in during its conversion:
        # 81 files stranded exactly that way, and the next sync took no-op time.
        # Erring EARLY costs one comparison; erring late loses data.
        _pre_push_unix = _time.time()
        result = await push(
            repo,
            data_path,
            parent=local_snapshot,
            excludes=excludes,
            box_index_name=index_name,
        )

        # Re-read the pointer immediately before publishing. If it no longer
        # names what this push was based on, another machine pushed while ours
        # ran: leave its pointer alone and report a conflict rather than
        # silently replacing its snapshot with ours. Our snapshot is safely in
        # the repository either way. This NARROWS the race; it does not remove
        # it, because rclone has no compare-and-swap.
        current = await read_pointer(
            config.rclone_config_path,
            box_meta.storage_location,
            store,
            remote_index_name,
        )
        if current is not None and current["snapshot"] != remote_snapshot:
            return (
                _status(
                    SyncCondition.CONFLICT,
                    local_exists=True,
                    remote_exists=True,
                    error=(
                        f"Another machine pushed '{index_name}' while this push "
                        f"was running ({remote_snapshot[:8]} -> "
                        f"{current['snapshot'][:8]}). This machine's work is "
                        f"safe in the repository as snapshot "
                        f"{result.snapshot_id[:8]}, but the box now needs a "
                        f"person to decide which is current."
                    ),
                ),
                False,
            )

        await write_pointer(
            config.rclone_config_path,
            box_meta.storage_location,
            store,
            remote_index_name,
            result.snapshot_id,
            result.source_path,
        )
        write_state(
            config.boxyard_data_path, index_name, result.snapshot_id,
            now_unix=_pre_push_unix,
            files=result.files,
        )
        _say(f"Pushed '{index_name}' as {result.snapshot_id[:8]}.")
        return _status(SyncCondition.NEEDS_PUSH, local_exists=True, remote_exists=True), True

    if condition is ResticCondition.CONFLICT:
        if not may_push:
            return (
                _status(
                    SyncCondition.WRITE_DENIED,
                    local_exists=True,
                    remote_exists=True,
                    error=write_denied_message(config, box_meta),
                ),
                False,
            )
        raise SyncUnsafe(
            f"Box '{index_name}' has diverged: the remote moved to "
            f"{remote_snapshot[:8]} and this machine has local DATA changes on "
            f"top of {(local_snapshot or 'nothing')[:8]}. Nothing has been "
            f"changed. `boxyard discard-local -r '{index_name}'` takes the "
            f"remote's version, keeping this machine's work in the repository "
            f"history."
        )

    raise ResticError(f"unhandled restic condition for '{index_name}': {condition}")

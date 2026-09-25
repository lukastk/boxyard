# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # multi_sync

# %%
#|default_exp _cli.multi_sync
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import typer
from typer import Option

from boxyard._enums import SyncSetting, SyncDirection, BoxPart
from boxyard._cli.app import app, app_state

# %%
#|export
from boxyard._models import get_boxyard_meta, SyncCondition
from boxyard.cmds import sync_box
from rich.live import Live
from rich.text import Text
from rich.console import Console
from datetime import datetime
import shutil

# %%
#|set_func_signature
@app.command(name="multi-sync")
def cli_multi_sync(
    box_index_names: list[str] | None = Option(
        None, "--box", "-r", help="The index names of the box, in the form."
    ),
    storage_locations: list[str] | None = Option(
        None, "--storage-location", "-s", help="The storage locations to sync."
    ),
    max_concurrent_rclone_ops: int | None = Option(
        None,
        "--max-concurrent",
        "-m",
        help="The maximum number of concurrent rclone operations. If not provided, the default specified in the config will be used.",
    ),
    sync_direction: SyncDirection | None = Option(
        None,
        "--sync-direction",
        help="The direction of the sync. If not provided, the appropriate direction will be automatically determined based on the sync status. This mode is only available for the 'CAREFUL' sync setting.",
    ),
    sync_setting: SyncSetting = Option(
        SyncSetting.CAREFUL, "--sync-setting", help="The sync setting to use."
    ),
    sync_choices: list[BoxPart] | None = Option(
        None,
        "--sync-choices",
        "-c",
        help="The parts of the box to sync. If not provided, all parts will be synced. By default, all parts are synced.",
    ),
    sync_recently_modified_first: bool = Option(
        False, help="Sync boxes that have been recently modified first."
    ),
    skip_unchanged_meta: bool = Option(
        False,
        "--skip-unchanged-meta",
        help=(
            "The META-only form of --skip-unchanged, for a `-c meta` pass: a box "
            "is put aside when its META is provably unchanged on both sides. "
            "Turns a 590-box META pass from ~1,180 remote calls into two "
            "listings plus the boxes that actually differ."
        ),
    ),
    skip_unchanged: bool = Option(
        False,
        "--skip-unchanged",
        help=(
            "Ask the remote ONCE, in bulk, which boxes moved, and put aside every "
            "box whose requested parts -- and the parts they depend on -- are "
            "provably unchanged on both sides: the remote sync record's md5, "
            "from one hashed listing, is the one this machine last agreed "
            "with, and the local tree matches the fingerprint baseline bound "
            "to it. Whatever is not provable goes through the real sync path, "
            "exactly as without the flag. Restic DATA keeps its snapshot-pointer "
            "check."
        ),
    ),
    due_only: bool = Option(
        False,
        "--due-only",
        help=(
            "Only sync boxes whose configured cadence says they are due, most "
            "overdue first. Requires [sync_policies.*] in config.toml; with "
            "none configured every box is due and this changes nothing."
        ),
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
    show_progress: bool = Option(True, help="Show the progress of the sync."),
    # `print_skipped`, not `no_print_skipped`. typer derives a bool option's
    # off-switch by prefixing "--no-", so the old name produced
    # `--no-no-print-skipped` for "actually do print them" -- a spelling nobody
    # would guess and the Go port quietly refused to reproduce, inventing
    # `--print-skipped` instead and diverging from this CLI without saying so.
    #
    # The rename keeps the spelling that reads properly: `--no-print-skipped`
    # still exists and still means what it always did. Only the double negative
    # is gone, replaced by `--print-skipped`.
    print_skipped: bool = Option(
        False, help="Print boxes for which no syncs happened."
    ),
    explain_skip: bool = Option(
        False,
        "--explain-skip",
        help=(
            "With --skip-unchanged[-meta]: print one line per box on stderr, "
            "`<box>: skipped` or `<box>: needed (<first gate that failed>)`, "
            "so a pass can be diffed against `boxyard doctor` and the oracle."
        ),
    ),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
):
    """
    Sync multiple boxes.
    """
    ...

# %% [markdown]
# Set up testing args

# %%
# Set up test environment
from tests.integration.conftest import create_boxyards

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# Create some boxes
from boxyard.cmds import new_box

for i in range(3):
    new_box(
        config_path=config_path,
        box_name=f"test_box_{i}",
        storage_location=remote_name,
    )

# %%
# Args
app_state = {"config_path": config_path}

box_index_names = None
storage_locations = None
max_concurrent_rclone_ops = None
sync_direction = None
sync_setting = SyncSetting.CAREFUL
sync_choices = None
sync_recently_modified_first = True
refresh_user_symlinks = True
show_progress = True
print_skipped = False
due_only = False
skip_unchanged_meta = False
skip_unchanged = False
explain_skip = False
soft_interruption_enabled = True

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
from boxyard._utils import enable_soft_interruption, SoftInterruption
from boxyard.config import get_config

if soft_interruption_enabled:
    enable_soft_interruption()

if box_index_names is not None and storage_locations is not None:
    typer.echo("Cannot provide both `--box` and `--storage-location`.", err=True)
    raise typer.Exit(code=1)

config = get_config(app_state["config_path"])

# The pass is driven by `asyncio.run` below. Called from inside a running
# loop, that cannot happen -- and the old code printed "syncing every box
# instead of filtering" and then executed NOTHING (reproduced by the
# implementation review: zero `sync_box` calls, exit 0). Refuse, loudly.
from boxyard._utils import is_in_event_loop as _in_loop

if _in_loop():
    raise RuntimeError(
        "multi-sync cannot run inside a running event loop: it drives its own "
        "with asyncio.run. Call it from a shell, a thread, or `sync_box` per box."
    )

if storage_locations is None and box_index_names is None:
    storage_locations = list(config.storage_locations.keys())
if storage_locations is not None and any(
    sl not in config.storage_locations for sl in storage_locations
):
    typer.echo(f"Invalid storage location: {storage_locations}", err=True)
    raise typer.Exit(code=1)

if max_concurrent_rclone_ops is None:
    max_concurrent_rclone_ops = config.max_concurrent_rclone_ops

if sync_choices is None:
    sync_choices = [part for part in BoxPart]

boxyard_meta = get_boxyard_meta(config)
if box_index_names is None:
    box_metas = [
        box_meta
        for box_meta in boxyard_meta.box_metas
        if box_meta.storage_location in storage_locations
    ]
else:
    if any(
        box_index_name not in boxyard_meta.by_index_name
        for box_index_name in box_index_names
    ):
        typer.echo(f"Non-existent box: {box_index_names}", err=True)
        raise typer.Exit(code=1)
    box_metas = [
        boxyard_meta.by_index_name[box_index_name]
        for box_index_name in box_index_names
    ]

# %%
#|export
# `--due-only` narrows the selection to boxes whose cadence says so. It is
# applied AFTER the explicit `--box` list on purpose: naming a box asks for that
# box, and a cadence is a default for the unattended loop, not an override of a
# person's explicit instruction.
_due_conflicts = []
if due_only:
    from boxyard._sync_policy import due_boxes as _due_boxes
    import time as _time

    _due = _due_boxes(config, box_metas, BoxPart.DATA, _time.time())
    _due_conflicts = _due.conflicts
    _due_order = {name: i for i, name in enumerate(_due.due)}
    box_metas = sorted(
        (bm for bm in box_metas if bm.index_name in _due_order),
        key=lambda bm: _due_order[bm.index_name],
    )

# %%
#|export
from boxyard._tombstones import list_tombstoned_box_ids
from boxyard.config import StorageType

_tombstoned_ids_by_sl: dict[str, set[str]] = {}


async def _load_tombstoned_ids():
    """
    Populate `_tombstoned_ids_by_sl`, one listing per rclone storage location.

    Idempotent: the skip filter loads it BEFORE deciding anything (a tombstoned
    box must never be put aside -- its warning has to print), and `_runner`
    calls it again for the unfiltered path. One listing either way.
    """
    if _tombstoned_ids_by_sl:
        return
    for _sl_name in sorted({bm.storage_location for bm in box_metas}):
        if config.storage_locations[_sl_name].storage_type == StorageType.LOCAL:
            continue  # a local store has no tombstones and needs no remote call
        _tombstoned_ids_by_sl[_sl_name] = await list_tombstoned_box_ids(
            config, _sl_name
        )

# Two bulk listings answer "did anything about this box move on the remote"
# for every box at once, instead of two remote calls per box per part. The
# `boxes/` listing (files AND directories at depth 2) gives boxmeta presence,
# whether a `conf/` directory exists, and the restic pointer; the hashed
# `sync_records/` listing gives every record's md5 -- its identity. Measured
# on the live storage box: ~13 s for `boxes/`, ~2.5 min for the 1,867 hashed
# records (serial `md5sum` per file; parallelism made it slower). Everything
# that survives the filter goes through the ordinary sync path.
if skip_unchanged_meta or skip_unchanged:
    # `asyncio` is imported further down this command body, which makes it a
    # local and leaves it unbound here. Same reason as StorageType above.
    import asyncio as _aio

    from boxyard import const as _const
    from boxyard._sync_policy import project_box_listing, project_record_listing
    from boxyard._utils import rclone_lsjson
    from boxyard._utils import CommandTimeout as _CommandTimeout
    from boxyard._utils.rclone import RcloneFailed as _RcloneFailed

    def _rclone_storage_locations():
        """Every rclone-backed store the selected boxes live on, by name."""
        from boxyard.config import StorageType

        return [
            _sl_name
            for _sl_name in sorted({bm.storage_location for bm in box_metas})
            if config.storage_locations[_sl_name].storage_type != StorageType.LOCAL
        ]

    async def _boxes_listing():
        """
        {(store, index name): RemoteBoxView} from ONE `lsjson` over `boxes/`
        per store. Files and directories at depth 2 -- the anchored `/*/`
        forms are kept over bare names, which at any other depth would admit
        a file INSIDE a box's DATA (measured: `b2/data/boxmeta.toml`), and
        `- **` stops the walk descending into `data/`. `+ /*/conf/` lists the
        conf DIRECTORY itself, so an empty or nested-only `conf/` counts as
        present (a files-only listing cannot prove a directory absent).
        """
        from boxyard import const

        view = {}
        for _sl_name in _rclone_storage_locations():
            _sl_conf = config.storage_locations[_sl_name]
            _entries = await rclone_lsjson(
                config.rclone_config_path,
                source=_sl_name,
                source_path=_sl_conf.store_path / const.REMOTE_BOXES_REL_PATH,
                recursive=True,
                filter=[
                    f"+ /*/{const.BOX_METAFILE_REL_PATH}",
                    f"+ /*/{const.BOX_SNAPSHOT_POINTER_REL_PATH}",
                    f"+ /*/{const.BOX_CONF_REL_PATH}/",
                    f"+ /*/{const.BOX_CONF_REL_PATH}",
                    f"+ /*/{const.BOX_DATA_REL_PATH}/",
                    f"+ /*/{const.BOX_DATA_REL_PATH}",
                    f"+ /*/{const.BOX_RESTIC_REL_PATH}/",
                    "- **",
                ],
                max_depth=2,
            )
            view.update(project_box_listing(_sl_name, _entries))
        return view

    async def _records_listing(only_names):
        """
        {(store, index name): {part: md5}} from ONE hashed `lsjson` over
        `sync_records/` per store. Depth-limited and anchored for the same
        reasons as above; exact keying is in `project_record_listing`.

        `only_names` restricts the walk to those record directories: a pass
        over a named handful of boxes (`--box`) must not hash every record
        the yard ever had to decide them. Its own, longer timeout: the walk
        grows with every box ever created, and growth must degrade to "slow",
        never to "silently off".
        """
        from boxyard import const

        if only_names is not None:
            _filters = [f"+ /{_n}/*.rec" for _n in sorted(only_names)] + ["- **"]
        else:
            _filters = ["+ /*/*.rec", "- **"]
        view = {}
        for _sl_name in _rclone_storage_locations():
            _sl_conf = config.storage_locations[_sl_name]
            _entries = await rclone_lsjson(
                config.rclone_config_path,
                source=_sl_name,
                source_path=_sl_conf.store_path / const.SYNC_RECORDS_REL_PATH,
                files_only=True,
                recursive=True,
                filter=_filters,
                max_depth=2,
                md5=True,
                timeout=const.RCLONE_HASHED_LISTING_TIMEOUT,
            )
            view.update(project_record_listing(_sl_name, _entries))
        return view

    async def _pointer_snapshots(views):
        """{index name: snapshot id or None} for the restic boxes in the pass:
        the pointer's CONTENT, read per box, is the DATA identity."""
        from boxyard._enums import StorageFormat
        from boxyard._restic import read_pointer
        from boxyard._sync_policy import remote_view_for

        async def _one(_bm):
            _view = remote_view_for(views, _bm.storage_location, _bm.box_id)
            if _view is None or not _view.pointer:
                return _bm.index_name, None
            _pointer = await read_pointer(
                config.rclone_config_path,
                _bm.storage_location,
                config.storage_locations[_bm.storage_location].store_path,
                _view.index_name,
            )
            return _bm.index_name, (_pointer or {}).get("snapshot")

        _restic = [bm for bm in box_metas if bm.storage_format is StorageFormat.RESTIC]
        _sem = _aio.Semaphore(max_concurrent_rclone_ops)

        async def _guarded(_bm):
            async with _sem:
                return await _one(_bm)

        return dict(await _aio.gather(*(_guarded(bm) for bm in _restic)))

    from boxyard._sync_policy import boxes_needing_sync_full

    # Tombstones first: a tombstoned box must reach `sync_box`, whose
    # warning is the only place the deletion is ever reported.
    _aio.run(_load_tombstoned_ids())
    _tombstoned_names = {
        bm.index_name
        for bm in box_metas
        if bm.box_id in _tombstoned_ids_by_sl.get(bm.storage_location, set())
    }

    # A listing that fails or times out costs the optimisation, never the
    # sync: say so and run the full pass. (The tombstone listing above is
    # different -- without it a box another machine deleted would be
    # resurrected -- and keeps raising.)
    try:
        _boxes_view = _aio.run(_boxes_listing())
        # A named selection hashes only its own record directories, under the
        # names the remote actually has for those boxes.
        _only_names = None
        if box_index_names is not None:
            from boxyard._sync_policy import remote_view_for as _rvf

            _only_names = {
                _v.index_name
                for _v in (_rvf(_boxes_view, bm.storage_location, bm.box_id) for bm in box_metas)
                if _v is not None
            }
        _records_view = _aio.run(_records_listing(_only_names))
        _pointers = _aio.run(_pointer_snapshots(_boxes_view))
    except (_RcloneFailed, _CommandTimeout) as _e:
        typer.echo(
            f"--skip-unchanged: a bulk listing failed, syncing every box this "
            f"pass instead of filtering ({_e})",
            err=True,
        )
        _boxes_view = None

    if _boxes_view is not None:
        # `--skip-unchanged` is the flag for the WHOLE box; `--skip-unchanged-meta`
        # is the META-only form for the fast loop. A part without its flag is
        # never provable, and a box is put aside only when every part in the
        # closure of what this pass would execute is proven -- see
        # `boxes_needing_sync_full` and the design note it implements.
        _verdicts = boxes_needing_sync_full(
            config,
            box_metas,
            requested_parts=sync_choices,
            records=_records_view,
            boxes=_boxes_view,
            tombstoned=_tombstoned_names,
            skip_meta=skip_unchanged or skip_unchanged_meta,
            skip_data=skip_unchanged,
            pointer_snapshots=_pointers,
        )
        _skippable = set(_verdicts.skippable)
        if explain_skip:
            for _bm in box_metas:
                if _bm.index_name in _skippable:
                    typer.echo(f"{_bm.index_name}: skipped", err=True)
                else:
                    typer.echo(
                        f"{_bm.index_name}: needed ({_verdicts.reasons.get(_bm.index_name, '?')})",
                        err=True,
                    )
        # A proven-unchanged box WAS checked: its cadence clock restarts too,
        # or `--due-only` would sort it first for ever (it never reaches the
        # real path's own check record).
        import time as _check_time

        from boxyard._sync_policy import SCHEDULABLE_PARTS as _SCHEDULABLE
        from boxyard._sync_policy import write_check_record as _write_check

        _checked_at = _check_time.time()
        for _bm in box_metas:
            if _bm.index_name in _skippable:
                for _part in sync_choices:
                    if _part in _SCHEDULABLE:
                        _write_check(config, _bm.index_name, _part, _checked_at)
        box_metas = [bm for bm in box_metas if bm.index_name not in _skippable]
        typer.echo(
            f"--skip-unchanged: {len(_skippable)} box(es) provably unchanged, "
            f"{len(_verdicts.needed)} going through the sync path.",
            err=True,
        )
        if not _skippable:
            # A pass that silently skipped nothing looks like a broken flag,
            # so say why: the part that failed to prove, per box, summarised.
            from collections import Counter as _Counter

            _why = _Counter(_verdicts.reasons.values())
            typer.echo(
                "--skip-unchanged: no box was skipped -- none was provably "
                f"unchanged (first unprovable part, by count: "
                f"{dict(_why.most_common(4))}).",
                err=True,
            )

# A box whose policies disagree is synced anyway -- the ambiguity is about how
# OFTEN, never about whether -- but it is never synced SILENTLY. Printed once
# per pass to stderr so an unattended loop's stdout stays parseable.
for _conflict in _due_conflicts:
    typer.echo(f"Sync policy conflict: {_conflict}", err=True)

# %% [markdown]
# ## Fetch the tombstones once, not once per box
#
# `sync_box` needs to know whether a box has been deleted from another machine.
# Asked per box that is one SFTP connection each -- 587 of them per pass, per
# machine, every 20 minutes. That saturated the storage box's connection limit
# and was failing ~8 boxes per pass on three machines with "couldn't initialise
# SFTP". One listing per storage location answers it for every box.
#
# A failure here is NOT survivable by carrying on: if we cannot tell which
# boxes are tombstoned, syncing anyway would resurrect a box another machine
# deleted. So it raises, naming the storage location. That is a smaller risk
# than it looks -- this is one call where there used to be 587, so the chance
# of hitting a transient failure at all is far lower than before.

# %% [markdown]
# Define syncing task

# %%
#|export
def _record_check(box_meta):
    """Record that every requested part of this box was checked just now."""
    import time as _time

    from boxyard._sync_policy import SCHEDULABLE_PARTS, write_check_record

    _now = _time.time()
    for _part in sync_choices:
        if _part in SCHEDULABLE_PARTS:
            write_check_record(config, box_meta.index_name, _part, _now)


async def _task(num, box_meta):
    sync_stats[box_meta.index_name] = (num, "Syncing...", None, datetime.now(), None)
    try:
        sync_results = await sync_box(
            config_path=app_state["config_path"],
            box_index_name=box_meta.index_name,
            sync_direction=sync_direction,
            sync_setting=sync_setting,
            sync_choices=sync_choices,
            tombstoned_box_ids=_tombstoned_ids_by_sl.get(box_meta.storage_location),
            verbose=False,
        )
        # A box this machine may not push is NOT an error, and must never be
        # rendered as one. `multi-sync` runs every 1200s under supervisor, so a
        # red line here would repeat ~72 times a day per machine for a state
        # that is working as designed and cannot be resolved by retrying --
        # exactly the noise the v0.4.x work existed to remove. It gets its own
        # status instead, and `doctor` explains it once with both ways out.
        _write_denied = any(
            status.sync_condition == SyncCondition.WRITE_DENIED
            for status, _ in sync_results.values()
        )
        # A box in a `local` storage location has no remote to sync against.
        # "Success" would be true but misleading, so it gets its own label --
        # for the same reason "Read-only" is not folded into "Error".
        _local_only = all(
            status.sync_condition == SyncCondition.LOCAL_STORAGE
            for status, _ in sync_results.values()
        )
        sync_stats[box_meta.index_name] = (
            num,
            "Read-only" if _write_denied else "Local" if _local_only else "Success",
            None,
            datetime.now(),
            sync_results,
        )
        # The box was CHECKED, so the cadence clock restarts -- including for
        # "Read-only" (write denied is a completed check whose answer was "not
        # yours to push") and "Local" (no remote to check against, but the
        # question was asked and answered).
        #
        # Deliberately NOT recorded for Error or Interrupted: an incomplete
        # check must leave the box due, so a box that fails every pass keeps
        # being retried instead of going quiet for its whole interval.
        _record_check(box_meta)
    except SoftInterruption:
        sync_stats[box_meta.index_name] = (
            num,
            "Interrupted",
            None,
            datetime.now(),
            None,
        )
    except Exception as e:
        sync_stats[box_meta.index_name] = (num, "Error", str(e), datetime.now(), None)

    if show_progress:
        print_finished(box_meta.index_name)

# %% [markdown]
# Set up the progress printing (shown if `show_progress == True`)

# %%

#|export
import asyncio

sync_stats = {}

finish_monitoring_event = asyncio.Event()


def get_status_lines(box_index_name):
    num, sync_stat, e, timestamp, sync_results = sync_stats[box_index_name]
    lines = []

    console_width = shutil.get_terminal_size((80, 20)).columns

    status_color = {
        # "Syncing...", with the dots. The key was "Syncing", which is not a
        # status any box ever has -- `_task` sets "Syncing..." -- so the live
        # board's in-flight lines rendered `[bold ]`: bold, and uncoloured.
        # rich accepts an empty style word without complaint, so nothing ever
        # surfaced it. `name_color` has no in-flight entry ON PURPOSE (the box
        # name stays plain until it has an outcome); this one is a typo.
        "Syncing...": "yellow",
        "Success": "green",
        "Read-only": "yellow",
        "Local": "blue",
        "Interrupted": "magenta",
        "Error": "red",
    }.get(sync_stat, "")

    name_color = {
        "Success": "green",
        "Read-only": "yellow",
        "Local": "blue",
        "Interrupted": "magenta",
        "Error": "red",
    }.get(sync_stat, "")

    left = f"({num + 1}/{len(box_metas)}) [bold {name_color}]{box_index_name}[/bold {name_color}]"
    right = f"[bold {status_color}]{sync_stat}[/bold {status_color}]"

    # Strip markup to compute the real visible lengths
    left_len = len(Text.from_markup(left).plain)
    right_len = len(Text.from_markup(right).plain)

    # compute how many dots are needed
    dots = (
        console_width - left_len - right_len - 1 - 2
    )  # -2 for the space between dots and the left and right text
    if dots < 1:
        dots = 1

    line = f"{left} {'.' * dots} {right}"
    syncs_happened = [
        False if sync_results is None else sync_results[box_part][1]
        for box_part in sync_choices
    ]
    lines.append(line)

    indent = "    "
    if e:
        lines.append(f"{indent}[red]{e}[/red]")
    elif sync_stat in ("Success", "Read-only", "Local"):
        line = []
        for box_part, synced in zip(sync_choices, syncs_happened):
            _denied = (
                sync_results is not None
                and sync_results[box_part][0].sync_condition
                == SyncCondition.WRITE_DENIED
            )
            if _denied:
                _cell = "[yellow]Write denied[/yellow]"
            elif synced:
                _cell = "[green]Synced[/green]"
            else:
                _cell = "[blue]Skipped[/blue]"
            line.append(f"[bold]{box_part.value}:[/bold] {_cell}")
        lines.append(indent + f",{indent}".join(line))
        if sync_stat == "Read-only":
            _owner = next(
                (
                    status.error_message
                    for status, _ in sync_results.values()
                    if status.sync_condition == SyncCondition.WRITE_DENIED
                ),
                None,
            )
            if _owner:
                lines.append(f"{indent}[yellow]{_owner}[/yellow]")
                lines.append(
                    f"{indent}[dim]`boxyard doctor` names both ways out.[/dim]"
                )
    else:
        lines.append(f"{indent}[yellow]Results pending...[/yellow]")

    return lines


def get_sync_stat_board(finished: bool):
    console_width = shutil.get_terminal_size((80, 20)).columns
    lines = []
    for box_index_name, (
        num,
        sync_stat,
        e,
        timestamp,
        sync_results,
    ) in sync_stats.items():
        if sync_stat != "Syncing...":
            continue
        lines.extend(get_status_lines(box_index_name))
    return "\n".join(lines).strip()


def print_finished(box_index_name: str):
    num, sync_stat, e, timestamp, sync_results = sync_stats[box_index_name]
    syncs_happened = [
        False if sync_results is None else sync_results[box_part][1]
        for box_part in sync_choices
    ]
    if (
        not print_skipped
        and sync_stat in ("Success", "Local")
        and not any(syncs_happened)
    ):
        return
    lines = get_status_lines(box_index_name)
    console.print(Text.from_markup("\n".join(lines).strip()))


console = Console()


async def _progress_monitor_task():
    with Live(console=console, refresh_per_second=4) as live:

        def _update_live(finished: bool):
            rendered = Text.from_markup(get_sync_stat_board(finished=finished))
            live.update(rendered)

        while not finish_monitoring_event.is_set():
            _update_live(False)
            await asyncio.sleep(0.2)
        live.update(Text.from_markup("Finished. Final results:\n\n"))

# %% [markdown]
# Run multi-sync

# %%
#|export
_box_metas = box_metas
# `--due-only` has already ordered boxes by overdue ratio. The generic
# recent-modification preference must not silently overwrite that schedule.
if sync_recently_modified_first and not due_only:
    from boxyard._utils import check_last_time_modified

    def get_last_modified(box_meta):
        last_modified = check_last_time_modified(box_meta.get_local_path(config))
        return last_modified.timestamp() if last_modified else 0

    _box_metas = sorted(_box_metas, key=get_last_modified, reverse=True)

from boxyard._utils import async_throttler
sync_task = async_throttler(
    [_task(num, box_meta) for num, box_meta in enumerate(_box_metas)],
    max_concurrency=max_concurrent_rclone_ops,
)


async def _runner():
    # Before any box is synced: if this raises we must not sync at all, since
    # we cannot tell which boxes another machine has deleted.
    await _load_tombstoned_ids()
    if show_progress:
        monitor_task = asyncio.create_task(_progress_monitor_task())
        await sync_task
        finish_monitoring_event.set()
        await monitor_task
    else:
        await sync_task

# %%
await _runner()

# %%
#|export
from boxyard._utils import is_in_event_loop

if not is_in_event_loop():
    asyncio.run(_runner())

final_sync_stat_board = get_sync_stat_board(finished=True)
console = Console()
console.print(final_sync_stat_board, markup=True)

if refresh_user_symlinks:
    from boxyard.cmds import create_user_symlinks

    create_user_symlinks(config_path=app_state["config_path"])

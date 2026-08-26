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

if storage_locations is None and box_index_names is None:
    storage_locations = list(config.storage_locations.keys())
if storage_locations is not None and any(
    sl not in config.storage_locations for sl in storage_locations
):
    typer.echo(f"Invalid storage location: {storage_locations}")
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
        typer.echo(f"Non-existent box: {box_index_names}")
        raise typer.Exit(code=1)
    box_metas = [
        boxyard_meta.by_index_name[box_index_name]
        for box_index_name in box_index_names
    ]

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

# %%
#|export
from boxyard._tombstones import list_tombstoned_box_ids
from boxyard.config import StorageType

_tombstoned_ids_by_sl: dict[str, set[str]] = {}


async def _load_tombstoned_ids():
    """Populate `_tombstoned_ids_by_sl`, one listing per rclone storage location."""
    for _sl_name in sorted({bm.storage_location for bm in box_metas}):
        if config.storage_locations[_sl_name].storage_type == StorageType.LOCAL:
            continue  # a local store has no tombstones and needs no remote call
        _tombstoned_ids_by_sl[_sl_name] = await list_tombstoned_box_ids(
            config, _sl_name
        )

# %% [markdown]
# Define syncing task

# %%
#|export
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
if sync_recently_modified_first:
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

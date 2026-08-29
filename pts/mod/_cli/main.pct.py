# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # main

# %%
#|default_exp _cli.main

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import typer
from typer import Argument, Option
from typing import Literal
from pathlib import Path
from enum import Enum
from boxyard._enums import SyncSetting, SyncDirection, BoxPart, RenameScope, SyncNameDirection
from boxyard._cli.app import app, app_state

# %% [markdown]
# ## Helpers for lock error handling

# %%
#|exporti
def _run_with_lock_handling(coro):
    """Run an async coroutine and handle LockAcquisitionError gracefully."""
    import asyncio
    from boxyard._utils.locking import LockAcquisitionError
    try:
        return asyncio.run(coro)
    except LockAcquisitionError as e:
        typer.echo(f"Error: {e}", err=True)
        typer.echo(
            "Hint: If you believe this is a stale lock, you can manually remove the lock file "
            f"at: {e.lock_path}",
            err=True
        )
        raise typer.Exit(code=1)


def _call_with_lock_handling(func, *args, **kwargs):
    """Call a function and handle LockAcquisitionError gracefully."""
    from boxyard._utils.locking import LockAcquisitionError
    try:
        return func(*args, **kwargs)
    except LockAcquisitionError as e:
        typer.echo(f"Error: {e}", err=True)
        typer.echo(
            "Hint: If you believe this is a stale lock, you can manually remove the lock file "
            f"at: {e.lock_path}",
            err=True
        )
        raise typer.Exit(code=1)

# %% [markdown]
# ## Main command

# %% [markdown]
# `--version` exists to be a rollout gate: rolling a change across the fleet
# means checking `ssh-target <machine> boxyard --version` on each, and until
# this existed the only way to ask was `pip show boxyard`, which does not work
# for a uv tool install. It reports the installed distribution's version, so it
# answers "what is actually installed here?" rather than "what does this source
# tree say?".

# %%
#|exporti
def _version_callback(value: bool) -> None:
    if not value:
        return
    import importlib.metadata

    typer.echo(importlib.metadata.version("boxyard"))
    raise typer.Exit()

# %%
#|export
@app.callback()
def entrypoint(
    ctx: typer.Context,
    config_path: Path | None = Option(
        None,
        "--config",
        help="The path to the config file. Will be '~/.config/boxyard/config.toml' if not provided.",
    ),
    version: bool = Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print the installed boxyard version and exit.",
    ),
):
    import os

    from boxyard import const

    # Config path precedence: the --config flag, then the BOXYARD_CONFIG_PATH
    # environment variable, then the default location.
    #
    # The env var used to be read in only two places, neither of which chose
    # the config the CLI loaded: `_utils/rclone.py` (to find `rclone_path`) and
    # a message in `init` telling the user to set it. So
    # `boxyard init --config <path>` instructed you to set a variable that the
    # CLI then silently ignored, and every subsequent command operated on the
    # default config instead.
    if config_path is not None:
        _resolved_config_path = config_path
    else:
        _env_config_path = os.environ.get(const.ENV_VAR_BOXYARD_CONFIG_PATH)
        _resolved_config_path = (
            Path(_env_config_path).expanduser()
            if _env_config_path
            else const.DEFAULT_CONFIG_PATH
        )
    app_state["config_path"] = _resolved_config_path
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(ctx.get_help())

# %%
# !boxyard

# %% [markdown]
# # Helpers

# %%
#|exporti
def _is_subsequence_match(term: str, name: str) -> bool:
    j = 0
    m = len(term)

    for ch in name:
        if j < m and ch == term[j]:
            j += 1
            if j == m:
                return True
    return j == m

# %%
assert _is_subsequence_match("lukas", "lukastk")
assert _is_subsequence_match("lukas", "I am lukastk")
assert _is_subsequence_match("ad", "abcd")
assert not _is_subsequence_match("acbd", "abcd")

# %%
#|exporti
class NameMatchMode(str, Enum):
    EXACT = "exact"
    CONTAINS = "contains"
    SUBSEQUENCE = "subsequence"


def _resolve_parent_box_id(parent: str) -> str:
    """
    Resolve `--parent` to a box id, or exit 1 saying it was not found.

    `--parent` is documented as taking an "index name, id, or name", and it
    only ever honoured the NAME: the value went in as `box_name`, which matches
    against `box_meta.name`, and an index name is never a substring of the bare
    name it ends with. So `--parent 20260601_ab12cd__thing` reported "Box not
    found."

    The two EXACT forms are tried first, then the name match. The order is
    unambiguous because an index name, a box id and a name have distinct
    shapes, and the first two are looked up by equality rather than substring.

    Called BEFORE the box is created, so a missing parent fails with nothing
    made.
    """
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config as _get_config

    _bm = get_boxyard_meta(_get_config(app_state["config_path"]))
    if parent in _bm.by_index_name:
        return _bm.by_index_name[parent].box_id
    if parent in _bm.by_id:
        return _bm.by_id[parent].box_id
    parent_index = _get_box_index_name(
        box_name=parent, box_id=None, box_index_name=None,
        name_match_mode=None, name_match_case=False,
    )
    parent_meta = _bm.by_index_name.get(parent_index)
    if parent_meta is None:
        typer.echo(f"Parent box '{parent}' not found.", err=True)
        raise typer.Exit(code=1)
    return parent_meta.box_id


def _checkout_marker(box_meta, config) -> str:
    return {
        "included": "●",
        "excluded": "○",
        "unavailable": "!",
        "missing": "×",
        "relocating": "↔",
    }[box_meta.get_checkout_status(config).state.value]


def _has_complete_checkout(box_meta, config) -> bool:
    from boxyard._checkout import LocalCheckoutState

    return box_meta.get_checkout_status(config).state == LocalCheckoutState.INCLUDED


def _can_include_checkout(box_meta, config) -> bool:
    from boxyard._checkout import LocalCheckoutState

    return box_meta.get_checkout_status(config).state in (
        LocalCheckoutState.EXCLUDED,
        LocalCheckoutState.MISSING,
    )


def _get_box_index_name(
    box_name: str | None,
    box_id: str | None,
    box_index_name: str | None,
    name_match_mode: NameMatchMode | None,
    name_match_case: bool,
    box_metas=None,
    pick_first: bool = False,
    allow_no_args: bool = True,
    label: str = "box",
) -> str:
    if not allow_no_args and (
        box_name is None
        and box_index_name is None
        and box_id is None
        and box_metas is None
    ):
        typer.echo(f"No {label} name, id or index name provided.", err=True)
        raise typer.Exit(code=1)

    from boxyard._models import BoxyardMeta

    if sum(1 for x in [box_name, box_index_name, box_id] if x is not None) > 1:
        raise typer.Exit(
            "Cannot provide more than one of `box-name`, `box-full-name` or `box-id`."
        )

    if name_match_mode is not None and box_name is None:
        raise typer.Exit(
            "`box-name` must be provided if `name-match-mode` is provided."
        )

    if pick_first and box_name is None:
        raise typer.Exit("`box-name` must be provided if `pick-first` is provided.")

    search_mode = (
        (box_id is None) and (box_name is None) and (box_index_name is None)
    )

    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    config = get_config(app_state["config_path"])
    if box_metas is None:
        box_metas = get_boxyard_meta(config).box_metas
    boxyard_meta = BoxyardMeta(box_metas=box_metas)

    if search_mode:
        # No selector at all: use the box the user is standing in.
        #
        # This USED to sit at the very end of the function, after the picker
        # branch below -- where it could never run, because search_mode always
        # entered that branch first. So `boxyard sync` typed inside a box
        # opened an fzf picker over the whole yard instead of syncing that box,
        # while the function still carried an error message ("could not be
        # inferred from current working directory") describing behaviour it did
        # not have.
        #
        # The candidate check is load-bearing: several callers pass a FILTERED
        # `box_metas` (`include` passes only excluded boxes, `exclude` only
        # eligible ones, `path` a group-filtered set). A cwd box outside that
        # set is not a valid answer for the command, so fall through to the
        # picker rather than returning something the command will reject.
        from boxyard._utils import get_box_index_name_from_sub_path

        _cwd_index_name = get_box_index_name_from_sub_path(
            config=config, sub_path=Path.cwd()
        )
        if _cwd_index_name is not None and _cwd_index_name in boxyard_meta.by_index_name:
            return _cwd_index_name

    if (box_id is not None or box_name is not None) or search_mode:
        if box_id is not None:
            if box_id not in boxyard_meta.by_id:
                raise typer.Exit(f"Box with id `{box_id}` not found.")
            box_index_name = boxyard_meta.by_id[box_id].index_name
        else:
            if box_name is not None:
                if name_match_mode is None:
                    name_match_mode = NameMatchMode.CONTAINS
                if name_match_mode == NameMatchMode.EXACT:
                    cmp = (
                        lambda x: x.name == box_name
                        if name_match_case
                        else x.name.lower() == box_name.lower()
                    )
                    boxes_with_name = [x for x in boxyard_meta.box_metas if cmp(x)]
                elif name_match_mode == NameMatchMode.CONTAINS:
                    cmp = (
                        lambda x: box_name in x.name
                        if name_match_case
                        else box_name.lower() in x.name.lower()
                    )
                    boxes_with_name = [x for x in boxyard_meta.box_metas if cmp(x)]
                elif name_match_mode == NameMatchMode.SUBSEQUENCE:
                    cmp = (
                        lambda x: _is_subsequence_match(box_name, x.name)
                        if name_match_case
                        else _is_subsequence_match(box_name.lower(), x.name.lower())
                    )
                    boxes_with_name = [x for x in boxyard_meta.box_metas if cmp(x)]
            else:
                boxes_with_name = boxyard_meta.box_metas

            boxes_with_name = sorted(boxes_with_name, key=lambda x: x.index_name)

            if len(boxes_with_name) == 0:
                typer.echo("Box not found.", err=True)
                raise typer.Exit(code=1)
            elif len(boxes_with_name) == 1:
                box_index_name = boxes_with_name[0].index_name
            else:
                if pick_first:
                    box_index_name = boxes_with_name[0].index_name
                else:
                    from boxyard._utils import run_fzf

                    _, box_index_name = run_fzf(
                        terms=[r.index_name for r in boxes_with_name],
                        disp_terms=[
                            f"{r.name} ({r.box_id}) groups: {', '.join(r.groups)}"
                            for r in boxes_with_name
                        ],
                    )
                    if box_index_name is None:
                        raise typer.Exit(code=0)

    return box_index_name

# %% [markdown]
# # `init`

# %%
#|export
@app.command(name="init")
def cli_init(
    config_path: Path | None = Option(
        None,
        "--config-path",
        help="The path to the config file. Will be ~/.config/boxyard/config.toml if not provided.",
    ),
    data_path: Path | None = Option(
        None,
        "--data-path",
        help="The path to the data directory. Will be ~/.boxyard if not provided.",
    ),
):
    """
    Initialise boxyard's config and data directories.
    """
    from boxyard.cmds import init_boxyard

    # `init` has its own `--config-path`, which predates the global `--config`.
    # Keep it (it is the documented flag for this command and takes
    # precedence), but fall back to the SAME resolution every other command
    # uses -- global `--config`, then BOXYARD_CONFIG_PATH, then the default --
    # instead of jumping straight to the default.
    #
    # Without this, `boxyard --config <sandbox> init` silently initialised the
    # DEFAULT config instead of the one named, which is exactly the class of
    # surprise the BOXYARD_CONFIG_PATH fix exists to remove.
    if config_path is None:
        config_path = app_state.get("config_path")

    init_boxyard(
        config_path=config_path,
        data_path=data_path,
        verbose=True,
    )

# %% [markdown]
# # `new`

# %%
#|export
@app.command(name="new")
def cli_new(
    storage_location: str | None = Option(
        None,
        "--storage-location",
        "-s",
        help="The storage location to create the new box in.",
    ),
    checkout_root: str | None = Option(
        None,
        "--checkout-root",
        help="Machine-local checkout root for DATA; defaults to the root named 'default'.",
    ),
    box_name: str | None = Option(
        None,
        "--box-name",
        "-n",
        help="The name of the box, the id or the path of the box.",
    ),
    from_path: Path | None = Option(
        None,
        "--from",
        "-f",
        help="Path to a local directory to move into boxyard as a new box.",
    ),
    copy_from_path: bool = Option(
        False,
        "--copy",
        "-c",
        help="Copy the contents of the from_path into the new box.",
    ),
    git_clone_url: str | None = Option(
        None,
        "--git-clone",
        help="Git URL (SSH or HTTPS) to clone as the new box.",
    ),
    creator_hostname: str | None = Option(
        None,
        "--creator-hostname",
        help="Used to explicitly set the creator hostname of the new box.",
    ),
    creation_timestamp_utc: str | None = Option(
        None,
        "--creation-timestamp-utc",
        help="The timestamp of the new box. Should be in the form '%Y%m%d_%H%M%S' (e.g. '20251116_105532') or '%Y%m%d' (e.g. '20251116'). If not provided, the current UTC timestamp will be used.",
    ),
    groups: list[str] | None = Option(
        None, "--group", "-g", help="The groups to add the new box to."
    ),
    parent: str | None = Option(
        None,
        "--parent",
        help="Parent box (index name, id, or name) to set for the new box.",
    ),
    initialise_git: bool = Option(
        True, help="Initialise a git box in the new box."
    ),
    claim: bool = Option(
        True,
        help="Make this machine the box's write owner. Use --no-claim when "
        "creating a box here that will be worked on elsewhere.",
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
):
    """
    Create a new box.
    """
    from boxyard.cmds import new_box
    from boxyard.cmds._new_box import _extract_box_name_from_git_url

    if box_name is None and from_path is not None:
        box_name = Path(from_path).name

    if box_name is None and git_clone_url is not None:
        box_name = _extract_box_name_from_git_url(git_clone_url)

    if box_name is None:
        typer.echo("No box name provided.", err=True)
        raise typer.Exit(code=1)

    if creation_timestamp_utc is not None:
        from datetime import datetime
        from boxyard import const

        try:
            creation_timestamp_utc = datetime.strptime(
                creation_timestamp_utc, const.BOX_TIMESTAMP_FORMAT
            )
        except ValueError:
            try:
                creation_timestamp_utc = datetime.strptime(
                    creation_timestamp_utc, const.BOX_TIMESTAMP_FORMAT_DATE_ONLY
                )
            except ValueError:
                typer.echo(f"Invalid creation timestamp: {creation_timestamp_utc}", err=True)
                raise typer.Exit(code=1)

    # Validate --group and --parent BEFORE creating anything.
    #
    # These used to be applied AFTER `new_box` returned, outside its
    # try/except + `_rollback_new_box`. So a bad group name or a missing parent
    # exited 1 with the box already fully created and registered, and a caller
    # that reads a non-zero exit as "nothing happened" accumulated orphans.
    # That is not hypothetical: a cockpit command built its `-g` list from
    # another command's output, one entry was an error string rather than a
    # group name, and the failed run left a real box behind.
    #
    # Both are knowable up front -- the group charset is a pure string check
    # and the parent either exists or does not -- so validating here is
    # cheaper than extending the rollback, and it also stops the index name
    # being echoed for a box the command then fails on.
    from boxyard._models import BoxMeta as _BoxMeta

    for _group in groups or []:
        _BoxMeta.validate_group_name(_group)

    _parent_box_id = None
    if parent:
        _parent_box_id = _resolve_parent_box_id(parent)

    box_index_name = _call_with_lock_handling(
        new_box,
        config_path=app_state["config_path"],
        storage_location=storage_location,
        checkout_root=checkout_root,
        box_name=box_name,
        from_path=from_path,
        copy_from_path=copy_from_path,
        creator_hostname=creator_hostname,
        initialise_git=initialise_git,
        creation_timestamp_utc=creation_timestamp_utc,
        verbose=False,
        git_clone_url=git_clone_url,
        claim=claim,
    )
    typer.echo(box_index_name)

    if groups:
        from boxyard.cmds import modify_boxmeta
        from boxyard.config import get_config

        config = get_config(app_state["config_path"])
        modify_boxmeta(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            modifications={
                "groups": config.default_box_groups + groups,
            },
        )

    if _parent_box_id is not None:
        from boxyard.cmds import modify_boxmeta as _modify_bm

        _modify_bm(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            modifications={"parents": [_parent_box_id]},
        )

    # `new` was the ONLY command that declared `--refresh-user-symlinks` and
    # then ignored it -- every other command guards the call. So
    # `--no-refresh-user-symlinks` rebuilt the symlink tree anyway, silently.
    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `sync`

# %%
#|export
@app.command(name="sync")
def cli_sync(
    box_path: Path | None = Option(
        None, "--box-path", "-p", help="The path to the box to sync."
    ),
    box_index_name: str | None = Option(
        None, "--box", "-r", help="The index name of the box."
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box to sync."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box to sync."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        help="Whether to match the box name case-sensitively.",
    ),
    sync_direction: SyncDirection | None = Option(
        None,
        "--sync-direction",
        "-d",
        help="The direction of the sync. If not provided, the appropriate direction will be automatically determined based on the sync status. This mode is only available for the 'CAREFUL' sync setting.",
    ),
    sync_setting: SyncSetting = Option(
        SyncSetting.CAREFUL, "--sync-setting", "-s", help="The sync setting to use."
    ),
    sync_choices: list[BoxPart] | None = Option(
        None,
        "--sync-choices",
        "-c",
        help="The parts of the box to sync. If not provided, all parts will be synced. By default, all parts are synced.",
    ),
    show_rclone_progress: bool = Option(
        False, "--progress", help="Show the progress of the sync in rclone."
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
    sync_children: bool = Option(
        False, "--sync-children", help="Also sync all descendant boxes after syncing the target.",
    ),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
):
    """
    Sync a box.
    """
    from boxyard.cmds import sync_box

    if box_path is not None:
        from boxyard._utils import get_box_index_name_from_sub_path
        from boxyard.config import get_config

        config = get_config(app_state["config_path"])
        box_index_name = get_box_index_name_from_sub_path(
            config=config,
            sub_path=box_path,
        )

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
    )

    if sync_choices is None:
        sync_choices = [box_part for box_part in BoxPart]

    _run_with_lock_handling(
        sync_box(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            sync_direction=sync_direction,
            sync_setting=sync_setting,
            sync_choices=sync_choices,
            verbose=True,
            show_rclone_progress=show_rclone_progress,
            soft_interruption_enabled=soft_interruption_enabled,
        )
    )

    if sync_children:
        from boxyard._models import get_boxyard_meta
        from boxyard.config import get_config as _get_config

        _config = _get_config(app_state["config_path"])
        _bm = get_boxyard_meta(_config)
        descendants = _bm.descendants_of(_bm.by_index_name[box_index_name].box_id)
        for desc in descendants:
            typer.echo(f"Syncing child: {desc.index_name}")
            _run_with_lock_handling(
                sync_box(
                    config_path=app_state["config_path"],
                    box_index_name=desc.index_name,
                    sync_direction=sync_direction,
                    sync_setting=sync_setting,
                    sync_choices=sync_choices,
                    verbose=True,
                    show_rclone_progress=show_rclone_progress,
                    soft_interruption_enabled=soft_interruption_enabled,
                )
            )

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `sync-missing-meta`

# %%
#|export
@app.command(name="sync-missing-meta")
def cli_sync_missing_meta(
    box_index_names: list[str] | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box, in the form '{ULID}__{BOX_NAME}'.",
    ),
    storage_locations: list[str] | None = Option(
        None,
        "--storage-location",
        "-s",
        help="The storage location to sync the metadata from.",
    ),
    sync_setting: SyncSetting = Option(
        SyncSetting.CAREFUL, "--sync-setting", help="The sync setting to use."
    ),
    sync_direction: SyncDirection | None = Option(
        None,
        "--sync-direction",
        "-d",
        help="The direction of the sync. If not provided, the appropriate direction will be automatically determined based on the sync status. This mode is only available for the 'CAREFUL' sync setting.",
    ),
    max_concurrent_rclone_ops: int | None = Option(
        None,
        "--max-concurrent",
        "-m",
        help="The maximum number of concurrent rclone operations. If not provided, the default specified in the config will be used.",
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
):
    """
    Syncs boxmeta on remote storage locations not yet present locally.
    """
    import asyncio
    from boxyard.cmds import sync_missing_boxmetas

    asyncio.run(
        sync_missing_boxmetas(
            config_path=app_state["config_path"],
            box_index_names=box_index_names,
            storage_locations=storage_locations,
            sync_setting=sync_setting,
            sync_direction=sync_direction,
            verbose=True,
            max_concurrent_rclone_ops=max_concurrent_rclone_ops,
            soft_interruption_enabled=soft_interruption_enabled,
        )
    )

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `add-to-group`

# %%
#|export
@app.command(name="add-to-group")
def cli_add_to_group(
    box_path: Path | None = Option(
        None, "--box-path", "-p", help="The path to the box to sync."
    ),
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box, in the form '{ULID}__{BOX_NAME}'.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box to sync."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box to sync."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        "-c",
        help="Whether to match the box name case-sensitively.",
    ),
    group_names: list[str] = Argument(
        ..., help="The group(s) to add the box to."
    ),
    sync_after: bool = Option(
        False,
        "--sync-after",
        "-s",
        help="Sync the box after adding it to the group.",
    ),
    sync_setting: SyncSetting = Option(
        SyncSetting.CAREFUL, "--sync-setting", help="The sync setting to use."
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
):
    """
    Add a box to one or more groups.
    """
    from boxyard.cmds import modify_boxmeta
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    if all([arg is None for arg in [box_path, box_index_name, box_id, box_name]]):
        box_path = Path.cwd()

    if box_path is not None:
        from boxyard._utils import get_box_index_name_from_sub_path

        config = get_config(app_state["config_path"])
        box_index_name = get_box_index_name_from_sub_path(
            config=config,
            sub_path=box_path,
        )
        if box_index_name is None:
            typer.echo(f"Box not found in `{box_path}`.", err=True)
            raise typer.Exit(code=1)

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
    )

    boxyard_meta = get_boxyard_meta(get_config(app_state["config_path"]))
    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)
    box_meta = boxyard_meta.by_index_name[box_index_name]
    new_groups = list(box_meta.groups)
    added = []
    for gn in group_names:
        if gn in new_groups:
            typer.echo(f"Box `{box_index_name}` already in group `{gn}`.")
        else:
            new_groups.append(gn)
            added.append(gn)
    if added:
        modify_boxmeta(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            modifications={"groups": new_groups},
        )

        if sync_after:
            import asyncio
            from boxyard.cmds import sync_box
            from boxyard._models import BoxPart

            asyncio.run(
                sync_box(
                    config_path=app_state["config_path"],
                    box_index_name=box_index_name,
                    sync_setting=sync_setting,
                    sync_direction=SyncDirection.PUSH,
                    sync_choices=[BoxPart.META],
                    verbose=True,
                    soft_interruption_enabled=soft_interruption_enabled,
                )
            )

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `remove-from-group`

# %%
#|export
@app.command(name="remove-from-group")
def cli_remove_from_group(
    box_path: Path | None = Option(
        None, "--box-path", "-p", help="The path to the box to sync."
    ),
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box, in the form '{ULID}__{BOX_NAME}'.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box to sync."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box to sync."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        "-c",
        help="Whether to match the box name case-sensitively.",
    ),
    group_names: list[str] = Argument(
        ..., help="The group(s) to remove the box from."
    ),
    sync_after: bool = Option(
        False,
        "--sync-after",
        "-s",
        help="Sync the box after removing it from the group.",
    ),
    sync_setting: SyncSetting = Option(
        SyncSetting.CAREFUL, "--sync-setting", help="The sync setting to use."
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
):
    """
    Remove a box from one or more groups.
    """
    from boxyard.cmds import modify_boxmeta
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    if all([arg is None for arg in [box_path, box_index_name, box_id, box_name]]):
        box_path = Path.cwd()

    if box_path is not None:
        from boxyard._utils import get_box_index_name_from_sub_path

        config = get_config(app_state["config_path"])
        box_index_name = get_box_index_name_from_sub_path(
            config=config,
            sub_path=box_path,
        )

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
    )

    boxyard_meta = get_boxyard_meta(get_config(app_state["config_path"]))
    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)
    box_meta = boxyard_meta.by_index_name[box_index_name]
    to_remove = set()
    for gn in group_names:
        if gn not in box_meta.groups:
            typer.echo(f"Box `{box_index_name}` not in group `{gn}`.")
        else:
            to_remove.add(gn)
    if to_remove:
        modify_boxmeta(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            modifications={"groups": [g for g in box_meta.groups if g not in to_remove]},
        )

        if sync_after:
            import asyncio
            from boxyard.cmds import sync_box
            from boxyard._models import BoxPart

            asyncio.run(
                sync_box(
                    config_path=app_state["config_path"],
                    box_index_name=box_index_name,
                    sync_setting=sync_setting,
                    sync_direction=SyncDirection.PUSH,
                    sync_choices=[BoxPart.META],
                    verbose=True,
                    soft_interruption_enabled=soft_interruption_enabled,
                )
            )

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `add-parent`

# %%
#|export
@app.command(name="add-parent")
def cli_add_parent(
    box_path: Path | None = Option(
        None, "--box-path", "-p", help="The path to the child box."
    ),
    box_index_name: str | None = Option(
        None, "--box", "-r", help="The index name of the child box."
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the child box."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the child box."
    ),
    parent_index_name: str | None = Option(
        None, "--parent", help="The index name of the parent box."
    ),
    parent_id: str | None = Option(
        None, "--parent-id", help="The id of the parent box."
    ),
    parent_name: str | None = Option(
        None, "--parent-name", help="The name of the parent box."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None, "--name-match-mode", "-m", help="The mode to use for matching box names.",
    ),
    name_match_case: bool = Option(
        False, "--name-match-case", "-c", help="Whether to match box names case-sensitively.",
    ),
    sync_after: bool = Option(
        False, "--sync-after", "-s", help="Sync the box meta after adding the parent.",
    ),
    sync_setting: SyncSetting = Option(
        SyncSetting.CAREFUL, "--sync-setting", help="The sync setting to use."
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
):
    """
    Add a parent to a box.
    """
    from boxyard.cmds import modify_boxmeta
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    if all(arg is None for arg in [box_path, box_index_name, box_id, box_name]):
        box_path = Path.cwd()

    if box_path is not None:
        from boxyard._utils import get_box_index_name_from_sub_path

        config = get_config(app_state["config_path"])
        box_index_name = get_box_index_name_from_sub_path(config=config, sub_path=box_path)
        if box_index_name is None:
            typer.echo(f"Box not found in `{box_path}`.", err=True)
            raise typer.Exit(code=1)

    box_index_name = _get_box_index_name(
        box_name=box_name, box_id=box_id, box_index_name=box_index_name,
        name_match_mode=name_match_mode, name_match_case=name_match_case,
    )

    # Resolve parent
    parent_index_name = _get_box_index_name(
        box_name=parent_name, box_id=parent_id, box_index_name=parent_index_name,
        name_match_mode=name_match_mode, name_match_case=name_match_case,
        allow_no_args=False, label="parent",
    )

    config = get_config(app_state["config_path"])
    boxyard_meta = get_boxyard_meta(config)

    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)
    if parent_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Parent box with index name `{parent_index_name}` not found.", err=True)
        raise typer.Exit(code=1)

    box_meta = boxyard_meta.by_index_name[box_index_name]
    parent_meta = boxyard_meta.by_index_name[parent_index_name]

    if parent_meta.box_id in box_meta.parents:
        typer.echo(f"Box `{box_index_name}` already has parent `{parent_index_name}`.")
    else:
        modify_boxmeta(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            modifications={"parents": [*box_meta.parents, parent_meta.box_id]},
        )

        if sync_after:
            import asyncio
            from boxyard.cmds import sync_box
            from boxyard._models import BoxPart

            asyncio.run(
                sync_box(
                    config_path=app_state["config_path"],
                    box_index_name=box_index_name,
                    sync_setting=sync_setting,
                    sync_direction=SyncDirection.PUSH,
                    sync_choices=[BoxPart.META],
                    verbose=True,
                    soft_interruption_enabled=soft_interruption_enabled,
                )
            )

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `remove-parent`

# %%
#|export
@app.command(name="remove-parent")
def cli_remove_parent(
    box_path: Path | None = Option(
        None, "--box-path", "-p", help="The path to the child box."
    ),
    box_index_name: str | None = Option(
        None, "--box", "-r", help="The index name of the child box."
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the child box."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the child box."
    ),
    parent_index_name: str | None = Option(
        None, "--parent", help="The index name of the parent box."
    ),
    parent_id: str | None = Option(
        None, "--parent-id", help="The id of the parent box."
    ),
    parent_name: str | None = Option(
        None, "--parent-name", help="The name of the parent box."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None, "--name-match-mode", "-m", help="The mode to use for matching box names.",
    ),
    name_match_case: bool = Option(
        False, "--name-match-case", "-c", help="Whether to match box names case-sensitively.",
    ),
    sync_after: bool = Option(
        False, "--sync-after", "-s", help="Sync the box meta after removing the parent.",
    ),
    sync_setting: SyncSetting = Option(
        SyncSetting.CAREFUL, "--sync-setting", help="The sync setting to use."
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
):
    """
    Remove a parent from a box.
    """
    from boxyard.cmds import modify_boxmeta
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    if all(arg is None for arg in [box_path, box_index_name, box_id, box_name]):
        box_path = Path.cwd()

    if box_path is not None:
        from boxyard._utils import get_box_index_name_from_sub_path

        config = get_config(app_state["config_path"])
        box_index_name = get_box_index_name_from_sub_path(config=config, sub_path=box_path)

    box_index_name = _get_box_index_name(
        box_name=box_name, box_id=box_id, box_index_name=box_index_name,
        name_match_mode=name_match_mode, name_match_case=name_match_case,
    )

    config = get_config(app_state["config_path"])
    boxyard_meta = get_boxyard_meta(config)

    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)

    box_meta = boxyard_meta.by_index_name[box_index_name]

    # Resolve the parent box_id to remove. When --parent-id is given explicitly we
    # use it directly and do NOT require the parent box to still exist — that is the
    # only way to drop a dangling parent left behind by a deleted parent box (the
    # `tree-orphans` check in `boxyard doctor` points here).
    if parent_id is not None:
        target_parent_id = parent_id
        parent_label = parent_id
    else:
        parent_index_name = _get_box_index_name(
            box_name=parent_name, box_id=parent_id, box_index_name=parent_index_name,
            name_match_mode=name_match_mode, name_match_case=name_match_case,
            allow_no_args=False, label="parent",
        )
        parent_meta = boxyard_meta.by_index_name.get(parent_index_name)
        target_parent_id = parent_meta.box_id if parent_meta else None
        parent_label = parent_index_name

    if target_parent_id is None or target_parent_id not in box_meta.parents:
        typer.echo(f"Box `{box_index_name}` does not have parent `{parent_label}`.", err=True)
        raise typer.Exit(code=1)
    else:
        modify_boxmeta(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            modifications={"parents": [p for p in box_meta.parents if p != target_parent_id]},
        )

        if sync_after:
            import asyncio
            from boxyard.cmds import sync_box
            from boxyard._models import BoxPart

            asyncio.run(
                sync_box(
                    config_path=app_state["config_path"],
                    box_index_name=box_index_name,
                    sync_setting=sync_setting,
                    sync_direction=SyncDirection.PUSH,
                    sync_choices=[BoxPart.META],
                    verbose=True,
                    soft_interruption_enabled=soft_interruption_enabled,
                )
            )

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `tree`

# %%
#|export
@app.command(name="tree")
def cli_tree(
    storage_locations: list[str] | None = Option(
        None, "--storage-location", "-s", help="The storage location to filter by.",
    ),
    include_groups: list[str] | None = Option(
        None, "--include-group", "-g", help="The group to include in the output."
    ),
    exclude_groups: list[str] | None = Option(
        None, "--exclude-group", "-e", help="The group to exclude from the output."
    ),
    group_filter: str | None = Option(
        None, "--group-filter", "-f", help="Boolean filter expression over groups.",
    ),
    root_box: str | None = Option(
        None, "--root", help="Show subtree from a specific box (index name, id, or name).",
    ),
    output_format: Literal["text", "json"] = Option(
        "text", "--output-format", "-o", help="The format of the output."
    ),
    show_status: bool = Option(
        False, "--show-status", help="Show included/excluded status icon (●/○) for each box.",
    ),
):
    """
    Display the parent-child hierarchy of boxes as a tree.
    """
    from boxyard._models import get_boxyard_meta, BoxyardMeta
    from boxyard.config import get_config
    import json

    config = get_config(app_state["config_path"])
    boxyard_meta = get_boxyard_meta(config)
    box_metas = list(boxyard_meta.box_metas)

    if storage_locations:
        box_metas = [bm for bm in box_metas if bm.storage_location in storage_locations]

    box_metas = _get_filtered_box_metas(box_metas, include_groups, exclude_groups, group_filter)

    filtered_meta = BoxyardMeta(box_metas=box_metas)
    filtered_ids = {bm.box_id for bm in box_metas}

    if output_format == "json":
        from boxyard._fast import BoxyardFast

        fast = BoxyardFast({"box_metas": [bm.model_dump() for bm in box_metas]})
        if root_box:
            # Resolve root_box to a box_id
            root_meta = filtered_meta.by_id.get(root_box) or filtered_meta.by_index_name.get(root_box)
            if root_meta is None:
                # Try name match
                matches = [bm for bm in box_metas if root_box in bm.name]
                if len(matches) == 1:
                    root_meta = matches[0]
                elif len(matches) > 1:
                    typer.echo(f"Ambiguous root box '{root_box}', matches: {[m.index_name for m in matches]}", err=True)
                    raise typer.Exit(code=1)
                else:
                    typer.echo(f"Root box '{root_box}' not found.", err=True)
                    raise typer.Exit(code=1)
            typer.echo(json.dumps(fast.get_dag_nested(root_meta.box_id), indent=2))
        else:
            typer.echo(json.dumps(fast.get_dag_nested(), indent=2))
        return

    # Text output using rich.tree
    from rich.tree import Tree as RichTree
    from rich.console import Console
    from rich.text import Text as RichText

    def _label(bm):
        # A `Text`, not a markup string. rich parses `[...]` as a style tag, so
        # the `[groups: ...]` suffix below was SWALLOWED whole -- `boxyard tree`
        # has never once shown a box's groups, and left a stray trailing space
        # where they should have been. Box NAMES have the same problem: one
        # containing `[` is valid (validate_box_name forbids only path
        # separators and the like) and would be mangled the same way.
        status = f"{_checkout_marker(bm, config)} " if show_status else ""
        groups_str = f" [groups: {', '.join(bm.groups)}]" if bm.groups else ""
        return RichText(f"{status}{bm.name} ({bm.box_id}){groups_str}")

    def _add_children(rich_node, parent_id):
        children = [bm for bm in box_metas if parent_id in bm.parents]
        children.sort(key=lambda x: x.index_name)
        for child in children:
            child_node = rich_node.add(_label(child))
            _add_children(child_node, child.box_id)

    root_metas = []
    if root_box:
        root_meta = filtered_meta.by_id.get(root_box) or filtered_meta.by_index_name.get(root_box)
        if root_meta is None:
            matches = [bm for bm in box_metas if root_box in bm.name]
            if len(matches) == 1:
                root_meta = matches[0]
            elif len(matches) > 1:
                typer.echo(f"Ambiguous root box '{root_box}', matches: {[m.index_name for m in matches]}", err=True)
                raise typer.Exit(code=1)
            else:
                typer.echo(f"Root box '{root_box}' not found.", err=True)
                raise typer.Exit(code=1)
        root_metas = [root_meta]
    else:
        root_metas = sorted(filtered_meta.roots(), key=lambda x: x.index_name)

    tree = RichTree("boxyard")
    # Track boxes shown in the tree so we can detect orphans
    shown_ids = set()

    for rm in root_metas:
        node = tree.add(_label(rm))
        shown_ids.add(rm.box_id)
        _add_children(node, rm.box_id)

    # Collect all shown descendants
    def _collect_shown(parent_id):
        for bm in box_metas:
            if parent_id in bm.parents and bm.box_id not in shown_ids:
                shown_ids.add(bm.box_id)
                _collect_shown(bm.box_id)

    for rm in root_metas:
        _collect_shown(rm.box_id)

    # Show orphaned children (parent not in filtered set)
    orphans = [bm for bm in box_metas if bm.box_id not in shown_ids]
    if orphans:
        # A `Text` for the same reason the labels are: as a markup string,
        # "[unknown parent]" is parsed as a style tag and vanishes, leaving a
        # bare "└── " above the orphans with nothing to explain them.
        unknown_node = tree.add(RichText("[unknown parent]"))
        for orphan in sorted(orphans, key=lambda x: x.index_name):
            child_node = unknown_node.add(_label(orphan))
            _add_children(child_node, orphan.box_id)

    Console().print(tree)

# %% [markdown]
# # `include`

# %%
#|export
@app.command(name="include")
def cli_include(
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box, in the form '{ULID}__{BOX_NAME}'.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box to sync."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box to sync."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        "-c",
        help="Whether to match the box name case-sensitively.",
    ),
    interactive: bool = Option(
        False,
        "--interactive",
        "-I",
        help="Interactively select multiple boxes to include.",
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
    read_only: bool = Option(
        False,
        "--read-only",
        help="Include without the nudge to claim an unowned box — for a box you only want to read here.",
    ),
    checkout_root: str | None = Option(
        None,
        "--checkout-root",
        help="Machine-local checkout root. Omit to reuse the box's remembered preference.",
    ),
):
    """
    Include a box in the local store.
    """
    from boxyard.cmds import include_box
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    if interactive:
        config = get_config(app_state["config_path"])
        boxyard_meta = get_boxyard_meta(config)

        # Filter to excluded boxes only
        excluded_boxes = [
            bm for bm in boxyard_meta.box_metas
            if _can_include_checkout(bm, config)
        ]
        excluded_boxes.sort(key=lambda bm: bm.name)

        if not excluded_boxes:
            typer.echo("No excluded boxes to include.")
            raise typer.Exit(code=0)

        from boxyard._utils import run_fzf_multi

        disp_terms = []
        for bm in excluded_boxes:
            group_tag = f" [{', '.join(bm.groups)}]" if bm.groups else ""
            disp_terms.append(f"\u25cb {bm.name} ({bm.box_id}){group_tag}")

        selections = run_fzf_multi(
            terms=[bm.index_name for bm in excluded_boxes],
            disp_terms=disp_terms,
        )
        if not selections:
            typer.echo("No boxes selected.")
            raise typer.Exit(code=0)

        # Confirmation
        typer.echo("Boxes to include:")
        for _, idx_name in selections:
            bm = boxyard_meta.by_index_name[idx_name]
            typer.echo(f"  {bm.name} ({bm.box_id})")
        if not typer.confirm(f"Include {len(selections)} box(es)?"):
            raise typer.Exit(code=0)

        errors = []
        for i, (_, idx_name) in enumerate(selections, 1):
            bm = boxyard_meta.by_index_name[idx_name]
            typer.echo(f"[{i}/{len(selections)}] Including {bm.name} ({bm.box_id})...")
            try:
                _run_with_lock_handling(
                    include_box(
                        config_path=app_state["config_path"],
                        box_index_name=idx_name,
                        soft_interruption_enabled=soft_interruption_enabled,
                        read_only=read_only,
                        checkout_root=checkout_root,
                    )
                )
            except (typer.Exit, SystemExit):
                raise
            except Exception as e:
                typer.echo(f"  Error: {e}", err=True)
                errors.append((bm.name, str(e)))
                continue

        if errors:
            typer.echo(f"\n{len(errors)} error(s):")
            for name, err in errors:
                typer.echo(f"  {name}: {err}", err=True)

        if refresh_user_symlinks:
            from boxyard.cmds import create_user_symlinks

            create_user_symlinks(config_path=app_state["config_path"])
        return

    config = get_config(app_state["config_path"])
    boxyard_meta = get_boxyard_meta(config)

    # Only show excluded boxes in fzf picker
    _eligible = [
        bm for bm in boxyard_meta.box_metas
        if _can_include_checkout(bm, config)
    ]

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
        box_metas=_eligible,
    )

    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)

    _run_with_lock_handling(
        include_box(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            soft_interruption_enabled=soft_interruption_enabled,
            read_only=read_only,
            checkout_root=checkout_root,
        )
    )

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `exclude`

# %%
#|export
@app.command(name="exclude")
def cli_exclude(
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box, in the form '{ULID}__{BOX_NAME}'.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box to sync."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box to sync."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        "-c",
        help="Whether to match the box name case-sensitively.",
    ),
    skip_sync: bool = Option(
        False,
        "--skip-sync",
        "-s",
        help="Skip the sync before excluding the box.",
    ),
    interactive: bool = Option(
        False,
        "--interactive",
        "-I",
        help="Interactively select multiple boxes to exclude.",
    ),
    show_sizes: bool = Option(
        False,
        "--show-sizes",
        help="Show local sizes of each box in interactive mode.",
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
):
    """
    Exclude a box from the local store.
    """
    from boxyard.cmds import exclude_box
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    if interactive:
        import os
        from boxyard._models import BoxPart
        from boxyard.config import StorageType

        config = get_config(app_state["config_path"])
        boxyard_meta = get_boxyard_meta(config)

        # Filter to included, non-local boxes only
        eligible_boxes = [
            bm for bm in boxyard_meta.box_metas
            if _has_complete_checkout(bm, config)
            and bm.get_storage_location_config(config).storage_type != StorageType.LOCAL
        ]

        if show_sizes:
            # Calculate sizes and sort by size descending
            def _get_dir_size(path):
                total = 0
                for dirpath, _dirnames, filenames in os.walk(path):
                    for f in filenames:
                        try:
                            total += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
                return total

            box_sizes = {}
            for bm in eligible_boxes:
                data_path = bm.get_local_part_path(config, BoxPart.DATA)
                box_sizes[bm.index_name] = _get_dir_size(data_path) if data_path.exists() else 0

            eligible_boxes.sort(key=lambda bm: box_sizes[bm.index_name], reverse=True)
        else:
            eligible_boxes.sort(key=lambda bm: bm.name)

        if not eligible_boxes:
            typer.echo("No eligible boxes to exclude.")
            raise typer.Exit(code=0)

        from boxyard._utils import run_fzf_multi

        disp_terms = []
        for bm in eligible_boxes:
            group_tag = f" [{', '.join(bm.groups)}]" if bm.groups else ""
            if show_sizes:
                size_bytes = box_sizes[bm.index_name]
                size_gb = size_bytes / (1024 ** 3)
                if size_gb >= 0.1:
                    size_str = f"[{size_gb:.1f} GB]"
                else:
                    size_mb = size_bytes / (1024 ** 2)
                    size_str = f"[{size_mb:.0f} MB]"
                disp_terms.append(f"{size_str}  \u25cf {bm.name} ({bm.box_id}){group_tag}")
            else:
                disp_terms.append(f"\u25cf {bm.name} ({bm.box_id}){group_tag}")

        selections = run_fzf_multi(
            terms=[bm.index_name for bm in eligible_boxes],
            disp_terms=disp_terms,
        )
        if not selections:
            typer.echo("No boxes selected.")
            raise typer.Exit(code=0)

        # Confirmation
        typer.echo("Boxes to exclude:")
        for _, idx_name in selections:
            bm = boxyard_meta.by_index_name[idx_name]
            size_info = ""
            if show_sizes:
                size_bytes = box_sizes[idx_name]
                size_gb = size_bytes / (1024 ** 3)
                if size_gb >= 0.1:
                    size_info = f"[{size_gb:.1f} GB]  "
                else:
                    size_mb = size_bytes / (1024 ** 2)
                    size_info = f"[{size_mb:.0f} MB]  "
            typer.echo(f"  {size_info}{bm.name} ({bm.box_id})")
        if show_sizes:
            total_bytes = sum(box_sizes[idx_name] for _, idx_name in selections)
            total_gb = total_bytes / (1024 ** 3)
            if total_gb >= 0.1:
                typer.echo(f"Total: {total_gb:.1f} GB")
            else:
                typer.echo(f"Total: {total_bytes / (1024 ** 2):.0f} MB")
        if not typer.confirm(f"Exclude {len(selections)} box(es)?"):
            raise typer.Exit(code=0)

        errors = []
        for i, (_, idx_name) in enumerate(selections, 1):
            bm = boxyard_meta.by_index_name[idx_name]
            typer.echo(f"[{i}/{len(selections)}] Excluding {bm.name} ({bm.box_id})...")
            try:
                _run_with_lock_handling(
                    exclude_box(
                        config_path=app_state["config_path"],
                        box_index_name=idx_name,
                        skip_sync=skip_sync,
                        soft_interruption_enabled=soft_interruption_enabled,
                    )
                )
            except (typer.Exit, SystemExit):
                raise
            except Exception as e:
                typer.echo(f"  Error: {e}", err=True)
                errors.append((bm.name, str(e)))
                continue

        if errors:
            typer.echo(f"\n{len(errors)} error(s):")
            for name, err in errors:
                typer.echo(f"  {name}: {err}", err=True)

        if refresh_user_symlinks:
            from boxyard.cmds import create_user_symlinks

            create_user_symlinks(config_path=app_state["config_path"])
        return

    config = get_config(app_state["config_path"])
    boxyard_meta = get_boxyard_meta(config)

    # Only show included, non-local boxes in fzf picker
    from boxyard.config import StorageType
    from boxyard._models import BoxPart
    _eligible = [
        bm for bm in boxyard_meta.box_metas
        if _has_complete_checkout(bm, config)
        and bm.get_storage_location_config(config).storage_type != StorageType.LOCAL
    ]

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
        box_metas=_eligible,
    )

    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)

    _run_with_lock_handling(
        exclude_box(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            skip_sync=skip_sync,
            soft_interruption_enabled=soft_interruption_enabled,
        )
    )

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `delete`

# %%
#|export
@app.command(name="delete")
def cli_delete(
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box, in the form '{ULID}__{BOX_NAME}'.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box to sync."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box to sync."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        "-c",
        help="Whether to match the box name case-sensitively.",
    ),
    force: bool = Option(
        False, "--force", help="Force deletion even if the box has children.",
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
):
    """
    Delete a box.
    """
    from boxyard.cmds import delete_box
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
        allow_no_args=False,
    )

    config = get_config(app_state["config_path"])
    boxyard_meta = get_boxyard_meta(config)
    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)

    box_meta = boxyard_meta.by_index_name[box_index_name]
    children = boxyard_meta.children_of(box_meta.box_id)
    if children and not force:
        child_names = ", ".join(c.index_name for c in children)
        typer.echo(
            f"Box `{box_index_name}` has children: {child_names}. "
            f"Use --force to delete anyway.",
            err=True,
        )
        raise typer.Exit(code=1)
    if children and force:
        child_names = ", ".join(c.index_name for c in children)
        typer.echo(
            f"Warning: Deleting box `{box_index_name}` will orphan children: {child_names}",
            err=True,
        )

    _run_with_lock_handling(
        delete_box(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            soft_interruption_enabled=soft_interruption_enabled,
        )
    )

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `box-status`

# %%
#|exporti
def _dict_to_hierarchical_text(
    data: dict, indents: int = 0, lines: list[str] = None
) -> list[str]:
    if lines is None:
        lines = []
    for k, v in data.items():
        if isinstance(v, dict):
            lines.append(f"{' ' * 4 * indents}{k}:")
            _dict_to_hierarchical_text(v, indents + 1, lines)
        else:
            lines.append(f"{' ' * 4 * indents}{k}: {v}")
    return lines

# %%
lines = _dict_to_hierarchical_text({"a": {"b": {"c": 1, "d": 2}, "e": 3}})
print("\n".join(lines))

# %%
#|exporti
async def get_formatted_box_status(config_path, box_index_name):
    from boxyard.cmds import get_box_sync_status
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config
    from boxyard._checkout import LocalCheckoutState
    from pydantic import BaseModel
    import json

    config = get_config(config_path)
    box_meta = get_boxyard_meta(config).by_index_name[box_index_name]
    checkout = box_meta.get_checkout_status(config)
    data = {"checkout": checkout.as_dict()}
    if checkout.state in (
        LocalCheckoutState.UNAVAILABLE,
        LocalCheckoutState.MISSING,
        LocalCheckoutState.RELOCATING,
    ):
        return data

    sync_status = await get_box_sync_status(
        config_path=config_path,
        box_index_name=box_index_name,
    )
    for box_part, part_sync_status in sync_status.items():
        part_sync_status_dump = part_sync_status._asdict()
        for k, v in part_sync_status_dump.items():
            if isinstance(v, BaseModel):
                part_sync_status_dump[k] = json.loads(v.model_dump_json())
            if isinstance(v, Enum):
                part_sync_status_dump[k] = v.value
        data[box_part.value] = part_sync_status_dump
    return data

# %%
#|export
@app.command(name="box-status")
def cli_box_status(
    box_path: Path | None = Option(
        None, "--box-path", "-p", help="The path to the box to sync."
    ),
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box, in the form '{ULID}__{BOX_NAME}'.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box to sync."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box to sync."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        "-c",
        help="Whether to match the box name case-sensitively.",
    ),
    output_format: Literal["text", "json"] = Option(
        "text", "--output-format", "-o", help="The format of the output."
    ),
    max_concurrent_rclone_ops: int | None = Option(
        None,
        "--max-concurrent",
        help="The maximum number of concurrent rclone operations. If not provided, the default specified in the config will be used.",
    ),
):
    """
    Get the sync status of a box.
    """
    import asyncio
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config
    import json

    if box_path is not None:
        from boxyard._utils import get_box_index_name_from_sub_path

        config = get_config(app_state["config_path"])
        box_index_name = get_box_index_name_from_sub_path(
            config=config,
            sub_path=box_path,
        )

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
    )

    boxyard_meta = get_boxyard_meta(get_config(app_state["config_path"]))
    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)

    sync_status_data = asyncio.run(
        get_formatted_box_status(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
        )
    )

    if output_format == "json":
        typer.echo(json.dumps(sync_status_data, indent=2))
    else:
        typer.echo("\n".join(_dict_to_hierarchical_text(sync_status_data)))

# %% [markdown]
# # `yard-status`

# %%
#|export
@app.command(name="yard-status")
def cli_yard_status(
    storage_locations: list[str] | None = Option(
        None,
        "--storage-location",
        "-s",
        help="The storage location to get the status of. If not provided, the status of all storage locations will be shown.",
    ),
    output_format: Literal["text", "json"] = Option(
        "text", "--output-format", "-o", help="The format of the output."
    ),
    max_concurrent_rclone_ops: int | None = Option(
        None,
        "--max-concurrent",
        "-m",
        help="The maximum number of concurrent rclone operations. If not provided, the default specified in the config will be used.",
    ),
):
    """
    Get the sync status of all boxes in the yard.
    """
    import asyncio
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config
    from boxyard._utils import async_throttler
    import json

    config = get_config(app_state["config_path"])
    if storage_locations is None:
        storage_locations = list(config.storage_locations.keys())
    if storage_locations is not None and any(
        sl not in config.storage_locations for sl in storage_locations
    ):
        typer.echo(f"Invalid storage location: {storage_locations}", err=True)
        raise typer.Exit(code=1)

    if max_concurrent_rclone_ops is None:
        max_concurrent_rclone_ops = config.max_concurrent_rclone_ops

    box_metas = [
        box_meta
        for box_meta in get_boxyard_meta(config).box_metas
        if box_meta.storage_location in storage_locations
    ]

    box_sync_statuses = asyncio.run(
        async_throttler(
            [
                get_formatted_box_status(app_state["config_path"], box_meta.index_name)
                for box_meta in box_metas
            ],
            max_concurrency=max_concurrent_rclone_ops,
        )
    )

    box_sync_statuses_by_sl = {}
    for box_sync_status, box_meta in zip(box_sync_statuses, box_metas):
        box_sync_statuses_by_sl.setdefault(box_meta.storage_location, {})[
            box_meta.index_name
        ] = box_sync_status

    if output_format == "json":
        typer.echo(json.dumps(box_sync_statuses_by_sl, indent=2))
    else:
        for sl_name, box_sync_statuses in box_sync_statuses_by_sl.items():
            typer.echo(f"{sl_name}:")
            typer.echo(
                "\n".join(_dict_to_hierarchical_text(box_sync_statuses, indents=1))
            )
            typer.echo("\n")

# %% [markdown]
# # `list`

# %%
#|exporti
def _get_filtered_box_metas(box_metas, include_groups, exclude_groups, group_filter):
    if include_groups:
        box_metas = [
            box_meta
            for box_meta in box_metas
            if any(group in box_meta.groups for group in include_groups)
        ]
    if exclude_groups:
        box_metas = [
            box_meta
            for box_meta in box_metas
            if not any(group in box_meta.groups for group in exclude_groups)
        ]
    if group_filter:
        from boxyard._utils.logical_expressions import get_group_filter_func

        _filter_func = get_group_filter_func(group_filter)
        box_metas = [
            box_meta for box_meta in box_metas if _filter_func(box_meta.groups)
        ]
    return box_metas

# %%
#|export
@app.command(name="list")
def cli_list(
    storage_locations: list[str] | None = Option(
        None,
        "--storage-location",
        "-s",
        help="The storage location to get the status of. If not provided, the status of all storage locations will be shown.",
    ),
    output_format: Literal["text", "json"] = Option(
        "text", "--output-format", "-o", help="The format of the output."
    ),
    include_groups: list[str] | None = Option(
        None, "--include-group", "-g", help="The group to include in the output."
    ),
    exclude_groups: list[str] | None = Option(
        None, "--exclude-group", "-e", help="The group to exclude from the output."
    ),
    group_filter: str | None = Option(
        None,
        "--group-filter",
        "-f",
        help="The filter to apply to the groups. The filter is a boolean expression over the groups of the boxes. Allowed operators are `AND`, `OR`, `NOT`, and parentheses for grouping..",
    ),
    children_of: str | None = Option(
        None, "--children-of", help="Filter to direct children of the given box.",
    ),
    descendants_of: str | None = Option(
        None, "--descendants-of", help="Filter to all descendants of the given box.",
    ),
    parent_of: str | None = Option(
        None, "--parent-of", help="Filter to direct parents of the given box.",
    ),
    ancestors_of: str | None = Option(
        None, "--ancestors-of", help="Filter to all ancestors of the given box.",
    ),
    roots_only: bool = Option(
        False, "--roots", help="Only show root boxes (no parents).",
    ),
    leaves_only: bool = Option(
        False, "--leaves", help="Only show leaf boxes (no children).",
    ),
    view: Literal["flat", "tree", "groups"] = Option(
        "flat", "--view", "-v", help="Display mode: flat list, parent-child tree, or grouped by group.",
    ),
    hide_groups: list[str] | None = Option(
        None, "--hide-group", help="Hide a group branch in --view groups. Boxes still appear under their other groups.",
    ),
    show_status: bool = Option(
        False, "--show-status", help="Show included/excluded status icon (●/○) for each box.",
    ),
    owner: str | None = Option(
        None,
        "--owner",
        help="Only show boxes whose write owner is this machine name. Pass 'none' for boxes with no owner.",
    ),
    show_owner: bool = Option(
        False, "--show-owner", help="Show each box's write owner.",
    ),
    checkout_root: str | None = Option(
        None, "--checkout-root", help="Only show boxes placed in this machine-local checkout root.",
    ),
    show_checkout: bool = Option(
        False, "--show-checkout", help="Show checkout state, root and authoritative local DATA path.",
    ),
):
    """
    List all boxes in the yard.
    """
    from boxyard._models import get_boxyard_meta, BoxyardMeta
    from boxyard.config import get_config
    import json

    config = get_config(app_state["config_path"])
    if storage_locations is None:
        storage_locations = list(config.storage_locations.keys())
    if storage_locations is not None and any(
        sl not in config.storage_locations for sl in storage_locations
    ):
        typer.echo(f"Invalid storage location: {storage_locations}", err=True)
        raise typer.Exit(code=1)

    all_boxyard_meta = get_boxyard_meta(config)
    box_metas = [
        box_meta
        for box_meta in all_boxyard_meta.box_metas
        if box_meta.storage_location in storage_locations
    ]
    if checkout_root is not None:
        if checkout_root not in config.configured_checkout_roots:
            typer.echo(f"Unknown checkout root: {checkout_root}", err=True)
            raise typer.Exit(code=1)
        box_metas = [
            bm for bm in box_metas
            if bm.get_checkout_status(config).checkout_root == checkout_root
        ]
    box_metas = _get_filtered_box_metas(
        box_metas, include_groups, exclude_groups, group_filter
    )

    # Hierarchy filters
    filtered_meta = BoxyardMeta(box_metas=box_metas)
    if children_of:
        ref = all_boxyard_meta.by_id.get(children_of) or all_boxyard_meta.by_index_name.get(children_of)
        if ref is None:
            matches = [bm for bm in all_boxyard_meta.box_metas if children_of in bm.name]
            ref = matches[0] if len(matches) == 1 else None
        if ref is None:
            typer.echo(f"Box '{children_of}' not found.", err=True)
            raise typer.Exit(code=1)
        child_ids = {c.box_id for c in all_boxyard_meta.children_of(ref.box_id)}
        box_metas = [bm for bm in box_metas if bm.box_id in child_ids]

    if descendants_of:
        ref = all_boxyard_meta.by_id.get(descendants_of) or all_boxyard_meta.by_index_name.get(descendants_of)
        if ref is None:
            matches = [bm for bm in all_boxyard_meta.box_metas if descendants_of in bm.name]
            ref = matches[0] if len(matches) == 1 else None
        if ref is None:
            typer.echo(f"Box '{descendants_of}' not found.", err=True)
            raise typer.Exit(code=1)
        desc_ids = {d.box_id for d in all_boxyard_meta.descendants_of(ref.box_id)}
        box_metas = [bm for bm in box_metas if bm.box_id in desc_ids]

    if parent_of:
        ref = all_boxyard_meta.by_id.get(parent_of) or all_boxyard_meta.by_index_name.get(parent_of)
        if ref is None:
            matches = [bm for bm in all_boxyard_meta.box_metas if parent_of in bm.name]
            ref = matches[0] if len(matches) == 1 else None
        if ref is None:
            typer.echo(f"Box '{parent_of}' not found.", err=True)
            raise typer.Exit(code=1)
        parent_ids = set(ref.parents)
        box_metas = [bm for bm in box_metas if bm.box_id in parent_ids]

    if ancestors_of:
        ref = all_boxyard_meta.by_id.get(ancestors_of) or all_boxyard_meta.by_index_name.get(ancestors_of)
        if ref is None:
            matches = [bm for bm in all_boxyard_meta.box_metas if ancestors_of in bm.name]
            ref = matches[0] if len(matches) == 1 else None
        if ref is None:
            typer.echo(f"Box '{ancestors_of}' not found.", err=True)
            raise typer.Exit(code=1)
        anc_ids = {a.box_id for a in all_boxyard_meta.ancestors_of(ref.box_id)}
        box_metas = [bm for bm in box_metas if bm.box_id in anc_ids]

    # 'none' is the only way to ask for "unowned" on a command line, since an
    # absent --owner already means "do not filter".
    if owner is not None:
        if owner.lower() == "none":
            box_metas = [bm for bm in box_metas if bm.write_owner is None]
        else:
            box_metas = [bm for bm in box_metas if bm.write_owner == owner]

    if roots_only:
        box_metas = [bm for bm in box_metas if len(bm.parents) == 0]

    if leaves_only:
        all_parent_ids = set()
        for bm in all_boxyard_meta.box_metas:
            all_parent_ids.update(bm.parents)
        box_metas = [bm for bm in box_metas if bm.box_id not in all_parent_ids]

    if view == "groups":
        from rich.tree import Tree as RichTree
        from rich.console import Console

        hidden = set(hide_groups or [])
        groups_map: dict[str, list] = {}
        ungrouped = []
        for bm in box_metas:
            visible_groups = [g for g in bm.groups if g not in hidden]
            if visible_groups:
                for g in visible_groups:
                    groups_map.setdefault(g, []).append(bm)
            else:
                ungrouped.append(bm)

        tree = RichTree("boxyard")
        # This view DOES want styling, so it stays a markup string -- but every
        # interpolated value is escaped now. The suffix was already escaped by
        # hand (`\\[`), which is what makes the two unescaped `[groups: ...]`
        # sites an oversight rather than a choice; the NAMES were not escaped,
        # and a box or group name may contain a bracket.
        from rich.markup import escape as _rich_escape

        for group_name in sorted(groups_map.keys()):
            group_node = tree.add(f"[bold]{_rich_escape(group_name)}[/bold]")
            for bm in sorted(groups_map[group_name], key=lambda x: x.name):
                other_groups = sorted(g for g in bm.groups if g != group_name)
                suffix = f" [dim]\\[{', '.join(other_groups)}][/dim]" if other_groups else ""
                status = f"{_checkout_marker(bm, config)} " if show_status else ""
                group_node.add(f"{status}{_rich_escape(bm.name)} ({bm.box_id}){suffix}")
        if ungrouped:
            ug_node = tree.add("[dim](ungrouped)[/dim]")
            for bm in sorted(ungrouped, key=lambda x: x.name):
                status = f"{_checkout_marker(bm, config)} " if show_status else ""
                suffix = f" [dim]\\[{', '.join(sorted(bm.groups))}][/dim]" if bm.groups else ""
                ug_node.add(f"{status}{_rich_escape(bm.name)} ({bm.box_id}){suffix}")

        Console().print(tree)
        return

    if view == "tree":
        from rich.tree import Tree as RichTree
        from rich.console import Console
        from rich.text import Text as RichText

        filtered_meta = BoxyardMeta(box_metas=box_metas)
        filtered_ids = {bm.box_id for bm in box_metas}

        def _label(bm):
            # See `boxyard tree`: a markup string here means rich eats the
            # `[groups: ...]` suffix, and any box name containing `[` with it.
            status = f"{_checkout_marker(bm, config)} " if show_status else ""
            groups_str = f" [groups: {', '.join(bm.groups)}]" if bm.groups else ""
            return RichText(f"{status}{bm.name} ({bm.box_id}){groups_str}")

        def _add_children(rich_node, parent_id, shown):
            children = [bm for bm in box_metas if parent_id in bm.parents]
            children.sort(key=lambda x: x.index_name)
            for child in children:
                if child.box_id not in shown:
                    shown.add(child.box_id)
                    child_node = rich_node.add(_label(child))
                    _add_children(child_node, child.box_id, shown)

        tree = RichTree("boxyard")
        shown = set()
        roots = sorted([bm for bm in box_metas if len(bm.parents) == 0 or not any(p in filtered_ids for p in bm.parents)], key=lambda x: x.index_name)
        for rm in roots:
            shown.add(rm.box_id)
            node = tree.add(_label(rm))
            _add_children(node, rm.box_id, shown)

        Console().print(tree)
        return

    def _list_record(box_meta):
        record = box_meta.model_dump()
        record.update(box_meta.get_checkout_status(config).as_dict())
        return record

    if output_format == "json":
        typer.echo(json.dumps([_list_record(rm) for rm in box_metas], indent=2))
    else:
        for box_meta in box_metas:
            checkout = box_meta.get_checkout_status(config)
            status_icons = {
                "included": "●",
                "excluded": "○",
                "unavailable": "!",
                "missing": "×",
                "relocating": "↔",
            }
            status = f"{status_icons[checkout.state.value]} " if show_status else ""
            owner_col = ""
            if show_owner:
                owner_col = f"  [{box_meta.write_owner or '-'}]"
            checkout_col = ""
            if show_checkout:
                checkout_col = (
                    f"  [{checkout.state.value} {checkout.checkout_root}:"
                    f"{checkout.local_path}]"
                )
            typer.echo(f"{status}{box_meta.index_name}{owner_col}{checkout_col}")

# %% [markdown]
# # `list-groups`

# %%
#|export
@app.command(name="list-groups")
def cli_list_groups(
    box_path: Path | None = Option(
        None,
        "--box-path",
        "-p",
        help="The path to the box to get the groups of.",
    ),
    box_index_name: str | None = Option(
        None, "--box", "-r", help="The box index name to get the groups of."
    ),
    list_all: bool = Option(
        False, "--all", "-a", help="List all groups, including virtual groups."
    ),
    include_virtual: bool = Option(
        False, "--include-virtual", "-v", help="Include virtual groups in the output."
    ),
):
    """
    List all groups a box belongs to, or all groups if `--all` is provided.
    """
    from boxyard._models import get_boxyard_meta, get_box_group_configs
    from boxyard.config import get_config

    config = get_config(app_state["config_path"])
    boxyard_meta = get_boxyard_meta(config)
    if box_index_name is not None and box_path is not None:
        typer.echo("Both --box and --box-path cannot be provided.", err=True)
        raise typer.Exit(code=1)

    if list_all and (box_path is not None or box_index_name is not None):
        typer.echo("Cannot provide both --box and --box-path when using --all.", err=True)
        raise typer.Exit(code=1)

    if list_all:
        group_configs, virtual_box_group_configs = get_box_group_configs(
            config, boxyard_meta.box_metas
        )
        groups = list(group_configs.keys())
        if include_virtual:
            groups.extend(virtual_box_group_configs.keys())
        for group_name in sorted(groups):
            typer.echo(group_name)
        return

    if box_index_name is None and box_path is None:
        box_path = Path.cwd()

    if box_path is not None:
        from boxyard._utils import get_box_index_name_from_sub_path

        box_index_name = get_box_index_name_from_sub_path(
            config=config,
            sub_path=box_path,
        )
        if box_index_name is None:
            typer.echo(
                "Could not determine the box index name from the provided box path."
            , err=True)
            raise typer.Exit(code=1)

    if box_index_name is None:
        typer.echo("Must provide box full name.", err=True)
        raise typer.Exit(code=1)

    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)
    box_meta = boxyard_meta.by_index_name[box_index_name]
    box_groups = box_meta.groups
    group_configs, virtual_box_group_configs = get_box_group_configs(
        config, [box_meta]
    )

    if include_virtual:
        for vg, vg_config in virtual_box_group_configs.items():
            if vg in group_configs:
                print(
                    f"Warning: Virtual box group '{vg}' is also a regular box group."
                )
            if vg_config.is_in_group(box_meta.groups):
                box_groups.append(vg)

    for group_name in sorted(box_groups):
        typer.echo(group_name)

# %% [markdown]
# # `path`

# %%
#|export
@app.command(name="path")
def cli_path(
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box, in the form '{ULID}__{BOX_NAME}'.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box to sync."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="What box path to show."
    ),
    pick_first: bool = Option(
        False,
        "--pick-first",
        "-1",
        help="Pick the first box if multiple boxes match the name.",
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        "-c",
        help="Whether to match the box name case-sensitively.",
    ),
    path_option: Literal[
        "data",
        "meta",
        "conf",
        "root",
        "sync-record-data",
        "sync-record-meta",
        "sync-record-conf",
    ] = Option(
        "data",
        "--path-option",
        "-p",
        help="The part of the box to get the path of.",
    ),
    include_groups: list[str] | None = Option(
        None, "--include-group", "-g", help="The group to include in the output."
    ),
    exclude_groups: list[str] | None = Option(
        None, "--exclude-group", "-e", help="The group to exclude from the output."
    ),
    all_boxes: bool = Option(
        False, "--all", "-a", help="Show all boxes, including non-included ones."
    ),
    group_filter: str | None = Option(
        None,
        "--group-filter",
        "-f",
        help="The filter to apply to the groups. The filter is a boolean expression over the groups of the boxes. Allowed operators are `AND`, `OR`, `NOT`, and parentheses for grouping..",
    ),
):
    """
    Get the path of a box.
    """
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    config = get_config(app_state["config_path"])
    boxyard_meta = get_boxyard_meta(config)
    box_metas = _get_filtered_box_metas(
        box_metas=boxyard_meta.box_metas,
        include_groups=include_groups,
        exclude_groups=exclude_groups,
        group_filter=group_filter,
    )

    if not all_boxes:
        box_metas = [rm for rm in box_metas if rm.check_included(config)]

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
        box_metas=box_metas,
        pick_first=pick_first,
    )

    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)
    box_meta = boxyard_meta.by_index_name[box_index_name]

    config = get_config(app_state["config_path"])

    if path_option == "data":
        typer.echo(box_meta.get_local_part_path(config, BoxPart.DATA).as_posix())
    elif path_option == "meta":
        typer.echo(box_meta.get_local_part_path(config, BoxPart.META).as_posix())
    elif path_option == "conf":
        typer.echo(box_meta.get_local_part_path(config, BoxPart.CONF).as_posix())
    elif path_option == "root":
        typer.echo(box_meta.get_local_path(config).as_posix())
    elif path_option == "sync-record-data":
        typer.echo(
            box_meta.get_local_sync_record_path(config, BoxPart.DATA).as_posix()
        )
    elif path_option == "sync-record-meta":
        typer.echo(
            box_meta.get_local_sync_record_path(config, BoxPart.META).as_posix()
        )
    elif path_option == "sync-record-conf":
        typer.echo(
            box_meta.get_local_sync_record_path(config, BoxPart.CONF).as_posix()
        )
    else:
        typer.echo(f"Invalid path option: {path_option}", err=True)
        raise typer.Exit(code=1)

# %% [markdown]
# # `create-user-symlinks`

# %%
#|export
@app.command(name="create-user-symlinks")
def cli_create_user_symlinks(
    user_box_groups_path: Path | None = Option(
        None,
        "--user-box-groups-path",
        "-g",
        help="The path to the user box groups. If not provided, the default specified in the config will be used.",
    ),
):
    """
    Rebuild group symlinks to authoritative checkout paths.
    """
    from boxyard.cmds import create_user_symlinks

    create_user_symlinks(
        config_path=app_state["config_path"],
        user_box_groups_path=user_box_groups_path,
    )

# %% [markdown]
# # `rename`

# %%
#|export
@app.command(name="rename")
def cli_rename(
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box to rename.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box to rename."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box to rename."
    ),
    new_name: str = Option(
        ..., "--new-name", "-N", help="The new name for the box."
    ),
    scope: RenameScope = Option(
        RenameScope.BOTH,
        "--scope",
        "-s",
        help="Where to rename: local, remote, or both.",
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        "-c",
        help="Whether to match the box name case-sensitively.",
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
):
    """
    Rename a box locally, on remote, or both.
    """
    from boxyard.cmds._rename_box import rename_box
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
        allow_no_args=False,
    )

    boxyard_meta = get_boxyard_meta(get_config(app_state["config_path"]))
    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)

    new_index_name = _run_with_lock_handling(
        rename_box(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            new_name=new_name,
            scope=scope,
            verbose=True,
        )
    )

    typer.echo(f"Renamed to: {new_index_name}")

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `sync-name`

# %%
#|export
@app.command(name="sync-name")
def cli_sync_name(
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box."
    ),
    to_local: bool = Option(
        False,
        "--to-local",
        help="Sync name from remote to local.",
    ),
    to_remote: bool = Option(
        False,
        "--to-remote",
        help="Sync name from local to remote.",
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        "-c",
        help="Whether to match the box name case-sensitively.",
    ),
    refresh_user_symlinks: bool = Option(True, help="Refresh the user symlinks."),
):
    """
    Sync the box name between local and remote.

    Must specify either --to-local or --to-remote (but not both).
    """
    from boxyard.cmds._sync_name import sync_name
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    if to_local == to_remote:
        typer.echo("Error: Must specify exactly one of --to-local or --to-remote.", err=True)
        raise typer.Exit(code=1)

    direction = SyncNameDirection.TO_LOCAL if to_local else SyncNameDirection.TO_REMOTE

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
        allow_no_args=False,
    )

    boxyard_meta = get_boxyard_meta(get_config(app_state["config_path"]))
    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)

    result_index_name = _run_with_lock_handling(
        sync_name(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            direction=direction,
            verbose=True,
        )
    )

    typer.echo(f"Result: {result_index_name}")

    if refresh_user_symlinks:
        from boxyard.cmds import create_user_symlinks

        create_user_symlinks(config_path=app_state["config_path"])

# %% [markdown]
# # `copy`

# %%
#|export
@app.command(name="copy")
def cli_copy(
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        help="Whether to match the box name case-sensitively.",
    ),
    dest_path: Path | None = Option(
        None, "--dest", "-d", help="Destination path for the copy. Defaults to ./BOX_NAME."
    ),
    copy_meta: bool = Option(
        False, "--meta", help="Also copy boxmeta.toml."
    ),
    copy_conf: bool = Option(
        False, "--conf", help="Also copy conf/ folder."
    ),
    overwrite: bool = Option(
        False, "--overwrite", help="Overwrite if dest exists."
    ),
    show_rclone_progress: bool = Option(
        False, "--progress", help="Show the progress of the copy in rclone."
    ),
):
    """
    Copy a remote box to a local path without including it.

    This downloads the box data to any local path without adding it to
    boxyard tracking, creating sync records, or making it an "included" box.
    """
    import asyncio
    from boxyard.cmds._copy_from_remote import copy_from_remote
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
        allow_no_args=False,
    )

    boxyard_meta = get_boxyard_meta(get_config(app_state["config_path"]))
    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)

    if dest_path is None:
        box_meta = boxyard_meta.by_index_name[box_index_name]
        dest_path = Path(box_meta.name)
        if dest_path.exists():
            typer.echo(f"Destination path `{dest_path}` already exists. Use --overwrite to overwrite.", err=True)
            raise typer.Exit(code=1)

    result_path = asyncio.run(
        copy_from_remote(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            dest_path=dest_path,
            copy_meta=copy_meta,
            copy_conf=copy_conf,
            overwrite=overwrite,
            show_rclone_progress=show_rclone_progress,
            verbose=True,
        )
    )

    typer.echo(f"Copied to: {result_path}")

# %% [markdown]
# # `force-push`

# %%
#|export
@app.command(name="force-push")
def cli_force_push(
    box_index_name: str | None = Option(
        None,
        "--box",
        "-r",
        help="The index name of the box.",
    ),
    box_id: str | None = Option(
        None, "--box-id", "-i", help="The id of the box."
    ),
    box_name: str | None = Option(
        None, "--box-name", "-n", help="The name of the box."
    ),
    name_match_mode: NameMatchMode | None = Option(
        None,
        "--name-match-mode",
        "-m",
        help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False,
        "--name-match-case",
        help="Whether to match the box name case-sensitively.",
    ),
    source_path: Path = Option(
        ..., "--source", "-s", help="Source folder to push."
    ),
    force: bool = Option(
        False, "--force", "-f", help="Required: confirm force overwrite."
    ),
    show_rclone_progress: bool = Option(
        False, "--progress", help="Show the progress of the sync in rclone."
    ),
    soft_interruption_enabled: bool = Option(True, help="Enable soft interruption."),
):
    """
    Force push a local folder to a box's remote DATA location.

    This is a destructive operation that overwrites the remote DATA with the
    contents of the source folder. Requires --force flag for safety.
    """
    from boxyard.cmds._force_push_to_remote import force_push_to_remote
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    box_index_name = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
        allow_no_args=False,
    )

    boxyard_meta = get_boxyard_meta(get_config(app_state["config_path"]))
    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{box_index_name}` not found.", err=True)
        raise typer.Exit(code=1)

    _run_with_lock_handling(
        force_push_to_remote(
            config_path=app_state["config_path"],
            box_index_name=box_index_name,
            source_path=source_path,
            force=force,
            show_rclone_progress=show_rclone_progress,
            soft_interruption_enabled=soft_interruption_enabled,
            verbose=True,
        )
    )

    typer.echo("Force push complete.")

# %% [markdown]
# # `which`

# %%
#|export
@app.command(name="which")
def cli_which(
    path: Path | None = Option(
        None, "--path", "-p", help="The path to check. Defaults to current working directory.",
    ),
    json_output: bool = Option(False, "--json", "-j", help="Output as JSON."),
    index_name_only: bool = Option(False, "--index-name", "-i", help="Only print the index name."),
):
    """
    Identify which box a path belongs to.
    """
    import json
    from boxyard._utils import get_box_index_name_from_sub_path
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    config = get_config(app_state["config_path"])
    target_path = path if path is not None else Path.cwd()

    box_index_name = get_box_index_name_from_sub_path(
        config=config,
        sub_path=target_path,
    )

    if box_index_name is None:
        typer.echo("Not inside a boxyard box.", err=True)
        raise typer.Exit(code=1)

    if index_name_only:
        typer.echo(box_index_name)
        return

    boxyard_meta = get_boxyard_meta(config)
    if box_index_name not in boxyard_meta.by_index_name:
        typer.echo(f"Box directory found ({box_index_name}) but no matching metadata.", err=True)
        raise typer.Exit(code=1)

    box_meta = boxyard_meta.by_index_name[box_index_name]

    checkout = box_meta.get_checkout_status(config)
    info = {
        "name": box_meta.name,
        "box_id": box_meta.box_id,
        "index_name": box_meta.index_name,
        "storage_location": box_meta.storage_location,
        "groups": box_meta.groups if box_meta.groups else [],
        "checkout_root": checkout.checkout_root,
        "local_data_path": checkout.local_path.as_posix(),
        "checkout_state": checkout.state.value,
        "root_available": checkout.root_status.available if checkout.root_status else False,
        "included": box_meta.check_included(config),
    }

    if json_output:
        typer.echo(json.dumps(info, indent=2))
    else:
        typer.echo(f"name: {info['name']}")
        typer.echo(f"box_id: {info['box_id']}")
        typer.echo(f"index_name: {info['index_name']}")
        typer.echo(f"storage_location: {info['storage_location']}")
        typer.echo(f"groups: {', '.join(info['groups']) if info['groups'] else '(none)'}")
        typer.echo(f"checkout_root: {info['checkout_root']}")
        typer.echo(f"local_data_path: {info['local_data_path']}")
        typer.echo(f"checkout_state: {info['checkout_state']}")
        typer.echo(f"included: {info['included']}")

# %% [markdown]
# # Checkout roots and local relocation

# %%
#|export
@app.command(name="checkout-roots")
def cli_checkout_roots(
    output_format: Literal["text", "json"] = Option(
        "text", "--output-format", "-o", help="The format of the output.",
    ),
):
    """List machine-local checkout roots and their verified availability."""
    import json
    from boxyard.config import get_config
    from boxyard._checkout import get_checkout_root_status

    config = get_config(app_state["config_path"])
    statuses = [
        get_checkout_root_status(config, name).as_dict()
        for name in sorted(config.configured_checkout_roots)
    ]
    if output_format == "json":
        typer.echo(json.dumps(statuses, indent=2))
        return
    for status in statuses:
        marker = "available" if status["available"] else "UNAVAILABLE"
        default = " (default)" if status["default"] else ""
        typer.echo(f"{status['name']}{default}: {status['path']} [{marker}]")
        if status["reason"]:
            typer.echo(f"  {status['reason']}")


# %%
#|export
@app.command(name="relocate")
def cli_relocate(
    box_index_name: str | None = Option(
        None, "--box", "-r", help="The index name of the box.",
    ),
    box_id: str | None = Option(None, "--box-id", "-i", help="The id of the box."),
    box_name: str | None = Option(None, "--box-name", "-n", help="The name of the box."),
    name_match_mode: NameMatchMode | None = Option(
        None, "--name-match-mode", "-m", help="The mode to use for matching the box name.",
    ),
    name_match_case: bool = Option(
        False, "--name-match-case", "-c", help="Match the box name case-sensitively.",
    ),
    checkout_root: str | None = Option(
        None, "--checkout-root", help="Destination checkout root. Omit only to recover an interrupted relocation.",
    ),
    adopt_existing: bool = Option(
        False, "--adopt-existing",
        help="Adopt a pre-populated destination after verifying it contains every source entry identically.",
    ),
):
    """Move an included checkout locally, or safely adopt an existing destination."""
    from boxyard.cmds._relocate_box import relocate_box

    resolved = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
        allow_no_args=False,
    )
    result = _call_with_lock_handling(
        relocate_box,
        config_path=app_state["config_path"],
        box_index_name=resolved,
        destination_root=checkout_root,
        adopt_existing=adopt_existing,
    )
    typer.echo(result)

# %% [markdown]
# # Write ownership: `claim`, `release`, `discard-local`, `owner`
#
# A box may be included on any number of machines, but exactly one machine at a
# time may PUSH its DATA. These four commands are how that owner is set, given
# up, worked around, and inspected.
#
# Every refusal below names an exact command that is safe to run verbatim, and
# `WRITE_DENIED` always names BOTH ways out — take the box over, or throw the
# local changes away. A refusal with one escape is a refusal people work around.

# %%
#|exporti
def _resolve_box(box_index_name, box_id, box_name, name_match_mode, name_match_case,
                 eligible=None, label="box"):
    """Shared box resolution for the ownership commands."""
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    config = get_config(app_state["config_path"])
    boxyard_meta = get_boxyard_meta(config)
    resolved = _get_box_index_name(
        box_name=box_name,
        box_id=box_id,
        box_index_name=box_index_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
        box_metas=eligible if eligible is not None else boxyard_meta.box_metas,
        label=label,
    )
    if resolved not in boxyard_meta.by_index_name:
        typer.echo(f"Box with index name `{resolved}` not found.", err=True)
        raise typer.Exit(code=1)
    return config, boxyard_meta, resolved


def _ownership_command(coro):
    """
    Run an ownership command, turning a refusal into a clean message + exit 1.

    `OwnershipRefused` carries a complete, actionable explanation, so printing a
    traceback on top of it would only bury the part the reader needs.
    """
    from boxyard._ownership import OwnershipRefused

    try:
        return _run_with_lock_handling(coro)
    except OwnershipRefused as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)

# %%
#|export
@app.command(name="claim")
def cli_claim(
    box_index_name: str | None = Option(
        None, "--box", "-r", help="The index name of the box, in the form '{ULID}__{BOX_NAME}'."
    ),
    box_id: str | None = Option(None, "--box-id", "-i", help="The id of the box."),
    box_name: str | None = Option(None, "--box-name", "-n", help="The name of the box."),
    name_match_mode: NameMatchMode | None = Option(
        None, "--name-match-mode", "-m", help="The mode to use for matching the box name."
    ),
    name_match_case: bool = Option(
        False, "--name-match-case", "-c", help="Whether to match the box name case-sensitively."
    ),
    steal: bool = Option(
        False, "--steal", help="Take the box from the machine that currently owns it."
    ),
    all_included: bool = Option(
        False, "--all-included", help="Claim every box included on this machine that has no owner."
    ),
    yes: bool = Option(False, "--yes", "-y", help="Skip the confirmation prompt for --steal."),
):
    """
    Make this machine the write owner of a box.

    Only the write owner may push a box's DATA. A box with no owner is
    unrestricted, exactly as before this feature existed.
    """
    from boxyard.cmds import claim_box
    from boxyard._models import get_boxyard_meta
    from boxyard.config import get_config

    if all_included:
        if steal:
            typer.echo(
                "--all-included and --steal cannot be combined: a bulk pass must "
                "never take boxes from other machines. Claim those one at a time, "
                "so each decision is visible.",
                err=True,
            )
            raise typer.Exit(code=1)

        from boxyard.config import StorageType

        config = get_config(app_state["config_path"])
        boxyard_meta = get_boxyard_meta(config)
        # Boxes in a LOCAL storage location are skipped rather than attempted
        # and refused: no other machine can reach them, so there is nothing to
        # coordinate, and listing every one of them as "needs a decision" would
        # bury the boxes that genuinely do.
        _here = [
            bm for bm in boxyard_meta.box_metas
            if _has_complete_checkout(bm, config)
            and config.storage_locations[bm.storage_location].storage_type
            == StorageType.RCLONE
        ]
        candidates = [bm for bm in _here if bm.write_owner is None]
        # Boxes included HERE but owned by ANOTHER machine are the whole reason
        # to run this as a pass rather than box by box: they are the boxes that
        # are genuinely on two machines at once, which nothing could enumerate
        # before. They are not claimed -- that would be a silent mass steal --
        # but they are listed, because a migration that quietly skipped them
        # would leave exactly the risk it was run to find.
        owned_elsewhere = [
            bm for bm in _here
            if bm.write_owner is not None and bm.write_owner != config.machine_name
        ]

        if not candidates and not owned_elsewhere:
            typer.echo("No unowned included boxes to claim.")
            raise typer.Exit(code=0)

        typer.echo(f"Claiming {len(candidates)} unowned box(es) included here.")
        failures = []
        for i, bm in enumerate(sorted(candidates, key=lambda b: b.index_name), 1):
            typer.echo(f"[{i}/{len(candidates)}] {bm.index_name}")
            try:
                _run_with_lock_handling(
                    claim_box(
                        config_path=app_state["config_path"],
                        box_index_name=bm.index_name,
                        verbose=False,
                    )
                )
            except (typer.Exit, SystemExit):
                raise
            except Exception as e:
                # A box included on two machines hits a META CONFLICT on the
                # second one. That is the point of the bulk pass: it enumerates
                # exactly the boxes that were at risk all along. Keep going and
                # list them at the end rather than stopping at the first.
                typer.echo(f"  Refused: {e}", err=True)
                failures.append((bm.index_name, str(e)))
        typer.echo("")
        typer.echo(f"Claimed {len(candidates) - len(failures)} of {len(candidates)}.")

        if owned_elsewhere:
            typer.echo("")
            typer.echo(
                f"{len(owned_elsewhere)} box(es) are included here but owned by "
                f"another machine — these are the boxes that were on two machines "
                f"at once:",
                err=True,
            )
            for bm in sorted(owned_elsewhere, key=lambda b: b.index_name):
                typer.echo(f"  {bm.index_name}  (owned by {bm.write_owner})", err=True)
            typer.echo(
                "For each: take it over with `boxyard claim --steal -r <box>`, drop "
                "this machine's copy with `boxyard discard-local -r <box>`, or stop "
                "holding it here with `boxyard exclude -r <box>`.",
                err=True,
            )

        if failures:
            typer.echo("", err=True)
            typer.echo(f"{len(failures)} box(es) could not be claimed:", err=True)
            for index_name, err in failures:
                typer.echo(f"  {index_name}: {err.splitlines()[0]}", err=True)

        if failures or owned_elsewhere:
            raise typer.Exit(code=1)
        return

    config, boxyard_meta, resolved = _resolve_box(
        box_index_name, box_id, box_name, name_match_mode, name_match_case,
        label="box to claim",
    )

    if steal and not yes:
        current = boxyard_meta.by_index_name[resolved].write_owner
        typer.echo(
            f"'{resolved}' is currently owned by '{current}'.\n"
            f"Taking it means any unpushed work for this box on '{current}' will "
            f"be REFUSED there from now on, and has to be recovered with "
            f"`boxyard discard-local` or by claiming it back."
        )
        if not typer.confirm(f"Take '{resolved}' from '{current}'?"):
            raise typer.Exit(code=0)

    _ownership_command(
        claim_box(
            config_path=app_state["config_path"],
            box_index_name=resolved,
            steal=steal,
        )
    )

# %%
#|export
@app.command(name="release")
def cli_release(
    box_index_name: str | None = Option(
        None, "--box", "-r", help="The index name of the box, in the form '{ULID}__{BOX_NAME}'."
    ),
    box_id: str | None = Option(None, "--box-id", "-i", help="The id of the box."),
    box_name: str | None = Option(None, "--box-name", "-n", help="The name of the box."),
    name_match_mode: NameMatchMode | None = Option(
        None, "--name-match-mode", "-m", help="The mode to use for matching the box name."
    ),
    name_match_case: bool = Option(
        False, "--name-match-case", "-c", help="Whether to match the box name case-sensitively."
    ),
):
    """
    Give up this machine's write ownership of a box.

    The tidy handover is `release` here, then `claim` on the machine that wants
    it — two online steps, no force and no race.
    """
    from boxyard.cmds import release_box

    _, _, resolved = _resolve_box(
        box_index_name, box_id, box_name, name_match_mode, name_match_case,
        label="box to release",
    )
    _ownership_command(
        release_box(config_path=app_state["config_path"], box_index_name=resolved)
    )

# %%
#|export
@app.command(name="discard-local")
def cli_discard_local(
    box_index_name: str | None = Option(
        None, "--box", "-r", help="The index name of the box, in the form '{ULID}__{BOX_NAME}'."
    ),
    box_id: str | None = Option(None, "--box-id", "-i", help="The id of the box."),
    box_name: str | None = Option(None, "--box-name", "-n", help="The name of the box."),
    name_match_mode: NameMatchMode | None = Option(
        None, "--name-match-mode", "-m", help="The mode to use for matching the box name."
    ),
    name_match_case: bool = Option(
        False, "--name-match-case", "-c", help="Whether to match the box name case-sensitively."
    ),
    yes: bool = Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    show_rclone_progress: bool = Option(False, "--progress", help="Show rclone progress."),
):
    """
    Replace this machine's copy of a box with the remote's.

    The other way out of a write-denied box, the first being
    `boxyard claim --steal`. What is overwritten is kept under the sync backups
    directory, and the path is printed.
    """
    from boxyard.cmds import discard_local

    config, _, resolved = _resolve_box(
        box_index_name, box_id, box_name, name_match_mode, name_match_case,
        label="box to discard local changes for",
    )

    if not yes:
        typer.echo(
            f"This replaces this machine's copy of '{resolved}' with the remote's.\n"
            f"What is overwritten is kept under '{config.local_sync_backups_path}', "
            f"so it is recoverable — but it will no longer be in the box."
        )
        if not typer.confirm(f"Discard this machine's changes to '{resolved}'?"):
            raise typer.Exit(code=0)

    _ownership_command(
        discard_local(
            config_path=app_state["config_path"],
            box_index_name=resolved,
            show_rclone_progress=show_rclone_progress,
        )
    )

# %%
#|export
@app.command(name="owner")
def cli_owner(
    box_index_name: str | None = Option(
        None, "--box", "-r", help="The index name of the box, in the form '{ULID}__{BOX_NAME}'."
    ),
    box_id: str | None = Option(None, "--box-id", "-i", help="The id of the box."),
    box_name: str | None = Option(None, "--box-name", "-n", help="The name of the box."),
    name_match_mode: NameMatchMode | None = Option(
        None, "--name-match-mode", "-m", help="The mode to use for matching the box name."
    ),
    name_match_case: bool = Option(
        False, "--name-match-case", "-c", help="Whether to match the box name case-sensitively."
    ),
    output_format: Literal["text", "json"] = Option(
        "text", "--output-format", "-o", help="The format of the output."
    ),
):
    """
    Show which machine may push a box's DATA.
    """
    import json

    config, boxyard_meta, resolved = _resolve_box(
        box_index_name, box_id, box_name, name_match_mode, name_match_case,
    )
    box_meta = boxyard_meta.by_index_name[resolved]
    owner = box_meta.write_owner
    info = {
        "index_name": resolved,
        "write_owner": owner,
        "this_machine": config.machine_name,
        "included_here": box_meta.check_included(config),
        "writable_here": owner is None or owner == config.machine_name,
    }

    if output_format == "json":
        typer.echo(json.dumps(info, indent=2))
        return

    if owner is None:
        typer.echo(
            f"'{resolved}' has no write owner, so any machine may push it "
            f"(the behaviour boxyard has always had)."
        )
    elif owner == config.machine_name:
        typer.echo(f"'{resolved}' is owned by this machine ({owner}).")
    else:
        typer.echo(
            f"'{resolved}' is owned by '{owner}'. This machine "
            f"({config.machine_name or 'unnamed'}) pulls it but never pushes it."
        )

# %% [markdown]
# # `doctor`

# %%
#|export
@app.command(name="doctor")
def cli_doctor(
    no_remote: bool = Option(
        False,
        "--no-remote",
        help="Skip checks that access remote storage (stale-meta-mirror, tombstoned-box and diverged-box), so doctor works offline.",
    ),
    storage_locations: list[str] | None = Option(
        None,
        "--storage-location",
        "-s",
        help="Restrict the remote checks to the given storage location(s). Local checks always cover all storage locations.",
    ),
    output_format: Literal["text", "json"] = Option(
        "text", "--output-format", "-o", help="The format of the output."
    ),
):
    """
    Run a strictly read-only health check of this machine's boxyard state.

    Checks: unregistered/malformed folders in the user boxes path, broken or
    duplicated registrations, a stale boxyard_meta.json cache, dangling group
    symlinks and debris in the group tree, orphaned sync records, interrupted
    syncs, leftovers from removed storage locations, rclone configuration,
    remote boxmetas missing from the local mirror and boxes tombstoned on the
    remote (both unless --no-remote), boxes referencing unknown parents,
    boxmetas or a config carrying keys written by a newer boxyard, and a
    missing `machine_name`.

    Never mutates or auto-fixes anything. Exit code is 0 when healthy and 1
    when there is at least one finding, so it can be asserted by cron jobs and
    agents.
    """
    import asyncio
    import json
    from boxyard.cmds import run_doctor
    from boxyard.config import get_config

    config = get_config(app_state["config_path"])
    if storage_locations is not None and any(
        sl not in config.storage_locations for sl in storage_locations
    ):
        typer.echo(f"Invalid storage location: {storage_locations}", err=True)
        raise typer.Exit(code=1)

    report = asyncio.run(
        run_doctor(
            config_path=app_state["config_path"],
            check_remote=not no_remote,
            storage_locations=storage_locations,
        )
    )

    if output_format == "json":
        typer.echo(json.dumps(report, indent=2))
    else:
        _max_listed = 10
        for check_name, check in report["checks"].items():
            if check["skipped"]:
                typer.echo(f"- {check_name}: skipped")
                continue
            findings = check["findings"]
            if not findings:
                typer.echo(f"✓ {check_name}: ok")
                continue
            typer.echo(f"✗ {check_name}: {len(findings)} finding(s)")
            for finding in findings:
                typer.echo(f"    - {finding['message']}")
                missing = finding.get("missing_index_names")
                if missing:
                    for index_name in missing[:_max_listed]:
                        typer.echo(f"        {index_name}")
                    if len(missing) > _max_listed:
                        typer.echo(
                            f"        ... and {len(missing) - _max_listed} more (use `-o json` for the full list)"
                        )
                typer.echo(f"      hint: {finding['hint']}")
        typer.echo("")
        if report["healthy"]:
            typer.echo("All checks passed.")
        else:
            typer.echo(f"{report['num_findings']} finding(s). See hints above.")

    if not report["healthy"]:
        raise typer.Exit(code=1)

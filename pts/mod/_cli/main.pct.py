# %% [markdown]
# # main

# %%
#|default_exp _cli.main

# %%
#|hide
import nblite; from nbdev.showdoc import show_doc; nblite.nbl_export()

# %%
#|export
import os
import typer
from typer import Argument, Option
from typing_extensions import Annotated
from types import FunctionType
from typing import Callable, Union, List
from pathlib import Path

import repoyard as proj
from repoyard import const
from repoyard.config import get_config
from repoyard._utils.sync_helper import SyncSetting, SyncDirection
from repoyard._models import RepoPart
from repoyard._cli.app import app, app_state


# %% [markdown]
# ## Main command

# %%
#|export
@app.callback()
def entrypoint(
    ctx: typer.Context,
    config_path: Path|None = Option(None, "--config", help=f"The path to the config file. Will be '{const.DEFAULT_CONFIG_PATH}' if not provided.")
):
    app_state['config_path'] = config_path if config_path is not None else const.DEFAULT_CONFIG_PATH
    if ctx.invoked_subcommand is not None: return
    typer.echo(ctx.get_help())


# %%
# !repoyard

# %% [markdown]
# # Helpers

# %%
#|exporti
def _get_full_repo_name(
    repo_name: str|None = None,
    repo_id: str|None = None,
    repo_full_name: str|None = None,
) -> str:
    if sum(1 for x in [repo_name, repo_full_name, repo_id] if x is not None) > 1:
        raise typer.Exit("Cannot provide more than one of `repo-name`, `repo-full-name` or `repo-id`.")
    
    if repo_id is not None or repo_name is not None:
        from repoyard._models import get_repoyard_meta
        config = get_config(app_state['config_path'])
        repoyard_meta = get_repoyard_meta(config)
        
        if repo_id is not None:
            if not repo_id in repoyard_meta.by_ulid:
                raise typer.Exit(f"Repository with id `{repo_id}` not found.")
            repo_full_name = repoyard_meta.by_ulid[repo_id].full_name
        else:
            repos_with_name = [x for x in repoyard_meta.by_full_name.values() if x.name == repo_name]
            if len(repos_with_name) == 0:
                raise typer.Exit(f"Repository with name `{repo_name}` not found.")
            elif len(repos_with_name) == 1:
                repo_full_name = repos_with_name[0].full_name
            else:
                from repoyard._utils import run_fzf
                _, repo_full_name = run_fzf(
                    terms=[r.full_name for r in repos_with_name],
                    disp_terms=[f"{r.name} ({r.ulid})" for r in repos_with_name],
                )
        
    if repo_full_name is None:
        from repoyard._utils import get_repo_full_name_from_cwd
        repo_full_name = get_repo_full_name_from_cwd(
            config=get_config(app_state['config_path']),
        )
        if repo_full_name is None:
            raise typer.Exit("Repo not specified and could not be inferred from current working directory.")
        
    return repo_full_name


# %% [markdown]
# # `init`

# %%
#|export
@app.command(name='init')
def cli_init(
    config_path: Path|None = Option(None, "--config-path", help=f"The path to the config file. Will be {const.DEFAULT_CONFIG_PATH} if not provided."),
    data_path: Path|None = Option(None, "--data-path", help=f"The path to the data directory. Will be {const.DEFAULT_DATA_PATH} if not provided."),
):
    """
    Create a new repository.
    """
    from repoyard.cmds import init_repoyard
    init_repoyard(
        config_path=config_path,
        data_path=data_path,
        verbose=True,
    )


# %% [markdown]
# # `new`

# %%
#|export
@app.command(name='new')
def cli_new(
    storage_location: str|None = None,
    repo_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, the id or the path of the repo."),
    from_path: Path|None = Option(None, "--from", "-f", help="Path to a local directory to move into repoyard as a new repository."),
    copy_from_path: bool = Option(False, "--copy", "-c", help="Copy the contents of the from_path into the new repository."),
    creator_hostname: str|None = Option(None, "--creator-hostname", "-c", help="Used to explicitly set the creator hostname of the new repository."),
    initialise_git: bool = Option(True, help="Initialise a git repository in the new repository."),
):
    """
    Create a new repository.
    """
    from repoyard.cmds import new_repo

    if repo_name is None and from_path is not None:
        repo_name = Path(from_path).name
        
    if repo_name is None:
        raise typer.Exit("No repository name provided.", code=1)
    
    new_repo(
        config_path=app_state['config_path'],
        storage_location=storage_location,
        repo_name=repo_name,
        from_path=from_path,
        copy_from_path=copy_from_path,
        creator_hostname=creator_hostname,
        initialise_git=initialise_git,
        verbose=True,
    )


# %% [markdown]
# # `sync`

# %%
#|export
@app.command(name='sync')
def cli_sync(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
    sync_direction: SyncDirection|None = Option(None, "--sync-direction", "-d", help="The direction of the sync. If not provided, the appropriate direction will be automatically determined based on the sync status. This mode is only available for the 'CAREFUL' sync setting."),
    sync_setting: SyncSetting = Option(SyncSetting.CAREFUL, "--sync-setting", "-s", help="The sync setting to use."),
    sync_choices: list[RepoPart] = Option(None, "--sync-choices", "-c", help="The parts of the repository to sync. If not provided, all parts will be synced. By default, all parts are synced."),
    show_rclone_progress: bool = Option(False, "--progress", "-p", help="Show the progress of the sync in rclone."),
):
    """
    Sync a repository.
    """
    from repoyard.cmds import sync_repo
    
    repo_full_name = _get_full_repo_name(
        repo_name=repo_name,
        repo_id=repo_id,
        repo_full_name=repo_full_name,
    )
    
    sync_repo(
        config_path=app_state['config_path'],
        repo_full_name=repo_full_name,
        sync_setting=sync_setting,
        force=sync_force,
        verbose=True,
        show_rclone_progress=show_rclone_progress,
    )


# %% [markdown]
# # `sync-meta`

# %%
#|export
@app.command(name='sync-meta')
def cli_sync_meta(
    repo_full_names: list[str]|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    storage_locations: list[str]|None = Option(None, "--storage-location", "-s", help="The storage location to sync the metadata from."),
    sync_all: bool = Option(False, "--all", "-a", help="Sync all repositories."),
    sync_setting: SyncSetting = Option(SyncSetting.CAREFUL, "--sync-setting", help="The sync setting to use."),
    sync_direction: SyncDirection|None = Option(None, "--sync-direction", "-d", help="The direction of the sync. If not provided, the appropriate direction will be automatically determined based on the sync status. This mode is only available for the 'CAREFUL' sync setting."),
):
    """
    Syncs the metadata of a repository.
    """
    from repoyard.cmds import sync_repometas
    
    if sync_all and (repo_full_names is not None or storage_locations is not None):
        raise typer.Exit("Cannot provide both `--all` and `--repo` or `--storage-location`.")

    if not sync_all and repo_full_names is None and storage_locations is None:
        from repoyard._utils import get_repo_full_name_from_cwd
        repo_full_name = get_repo_full_name_from_cwd(
            config=get_config(app_state['config_path']),
        )
        if repo_full_name is None:
            raise typer.Exit("Repo names to sync not specified and could not be inferred from current working directory.")
        repo_full_names = [repo_full_name]

    sync_repometas(
        config_path=app_state['config_path'],
        repo_full_names=repo_full_names,
        storage_locations=storage_locations,
        sync_setting=sync_setting,
        sync_direction=sync_direction,
        verbose=True,
    )


# %% [markdown]
# # `add-to-group`

# %%
#|export
@app.command(name='add-to-group')
def cli_add_to_group(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
    group_name: str = Option(..., "--group-name", "-g", help="The name of the group to add the repository to."),
    sync_after: bool = Option(True, "--sync-after", "-s", help="Sync the repository after adding it to the group."),
    sync_setting: SyncSetting = Option(SyncSetting.CAREFUL, "--sync-setting", help="The sync setting to use."),
):
    """
    Modify the metadata of a repository.
    """
    from repoyard.cmds import modify_repometa
    from repoyard._models import get_repoyard_meta
    
    repo_full_name = _get_full_repo_name(
        repo_name=repo_name,
        repo_id=repo_id,
        repo_full_name=repo_full_name,
    )
    
    repoyard_meta = get_repoyard_meta(get_config(app_state['config_path']))
    if repo_full_name not in repoyard_meta.by_full_name:
        raise typer.Exit(f"Repository with full name `{repo_full_name}` not found.", code=1)
    repo_meta = repoyard_meta.by_full_name[repo_full_name]
    if group_name in repo_meta.groups:
        raise typer.echo(f"Repository `{repo_full_name}` already in group `{group_name}`.")
    else:
        modify_repometa(
            config_path=app_state['config_path'],
            repo_full_name=repo_full_name,
            modifications={
                'groups': [*repo_meta.groups, group_name]
            }
        )
        
        if sync_after:
            from repoyard.cmds import sync_repometas
            sync_repometas(
                config_path=app_state['config_path'],
                repo_full_names=[repo_full_name],
                sync_setting=sync_setting,
                sync_direction=SyncDirection.PUSH,
                verbose=True,
            )


# %% [markdown]
# # `remove-from-group`

# %%
#|export
@app.command(name='remove-from-group')
def cli_remove_from_group(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
    group_name: str = Option(..., "--group-name", "-g", help="The name of the group to add the repository to."),
    sync_after: bool = Option(True, "--sync-after", "-s", help="Sync the repository after adding it to the group."),
    sync_setting: SyncSetting = Option(SyncSetting.CAREFUL, "--sync-setting", help="The sync setting to use."),
):
    """
    Modify the metadata of a repository.
    """
    from repoyard.cmds import modify_repometa
    from repoyard._models import get_repoyard_meta
    
    repo_full_name = _get_full_repo_name(
        repo_name=repo_name,
        repo_id=repo_id,
        repo_full_name=repo_full_name,
    )
    
    repoyard_meta = get_repoyard_meta(get_config(app_state['config_path']))
    if repo_full_name not in repoyard_meta.by_full_name:
        raise typer.Exit(f"Repository with full name `{repo_full_name}` not found.", code=1)
    repo_meta = repoyard_meta.by_full_name[repo_full_name]
    if group_name not in repo_meta.groups:
        raise typer.echo(f"Repository `{repo_full_name}` not in group `{group_name}`.")
    else:
        modify_repometa(
            config_path=app_state['config_path'],
            repo_full_name=repo_full_name,
            modifications={
                'groups': [g for g in repo_meta.groups if g != group_name]
            }
        )
        
        if sync_after:
            from repoyard.cmds import sync_repometas
            sync_repometas(
                config_path=app_state['config_path'],
                repo_full_names=[repo_full_name],
                sync_setting=sync_setting,
                sync_direction=SyncDirection.PUSH,
                verbose=True,
            )


# %% [markdown]
# # `include`

# %%
#|export
@app.command(name='include')
def cli_include(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
):
    """
    Include a repository in the local store.
    """
    from repoyard.cmds import include_repo
    from repoyard._models import get_repoyard_meta
    
    repo_full_name = _get_full_repo_name(
        repo_name=repo_name,
        repo_id=repo_id,
        repo_full_name=repo_full_name,
    )
    
    repoyard_meta = get_repoyard_meta(get_config(app_state['config_path']))
    if repo_full_name not in repoyard_meta.by_full_name:
        raise typer.Exit(f"Repository with full name `{repo_full_name}` not found.", code=1)
    
    include_repo(
        config_path=app_state['config_path'],
        repo_full_name=repo_full_name,
    )


# %% [markdown]
# # `exclude`

# %%
#|export
@app.command(name='exclude')
def cli_exclude(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
    skip_sync: bool = Option(False, "--skip-sync", "-s", help="Skip the sync before excluding the repository."),
):
    """
    Exclude a repository from the local store.
    """
    from repoyard.cmds import exclude_repo
    from repoyard._models import get_repoyard_meta
    
    repo_full_name = _get_full_repo_name(
        repo_name=repo_name,
        repo_id=repo_id,
        repo_full_name=repo_full_name,
    )
    
    repoyard_meta = get_repoyard_meta(get_config(app_state['config_path']))
    if repo_full_name not in repoyard_meta.by_full_name:
        raise typer.Exit(f"Repository with full name `{repo_full_name}` not found.", code=1)
    
    exclude_repo(
        config_path=app_state['config_path'],
        repo_full_name=repo_full_name,
        skip_sync=skip_sync,
    )


# %% [markdown]
# # `delete`

# %%
#|export
@app.command(name='delete')
def cli_delete(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
):
    """
    Delete a repository.
    """
    from repoyard.cmds import delete_repo
    from repoyard._models import get_repoyard_meta
    
    repo_full_name = _get_full_repo_name(
        repo_name=repo_name,
        repo_id=repo_id,
        repo_full_name=repo_full_name,
    )
    
    repoyard_meta = get_repoyard_meta(get_config(app_state['config_path']))
    if repo_full_name not in repoyard_meta.by_full_name:
        raise typer.Exit(f"Repository with full name `{repo_full_name}` not found.", code=1)
    
    delete_repo(
        config_path=app_state['config_path'],
        repo_full_name=repo_full_name,
    )

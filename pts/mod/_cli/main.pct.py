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
from typing import Callable, Union, List, Literal
from pathlib import Path
from enum import Enum
import asyncio

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
class NameMatchMode(Enum):
    EXACT = "exact"
    CONTAINS = "contains"
    SUBSEQUENCE = "subsequence"

def _get_full_repo_name(
    repo_name: str|None,
    repo_id: str|None,
    repo_full_name: str|None,
    name_match_mode: NameMatchMode|None,
    name_match_case: bool,
) -> str:
    if sum(1 for x in [repo_name, repo_full_name, repo_id] if x is not None) > 1:
        raise typer.Exit("Cannot provide more than one of `repo-name`, `repo-full-name` or `repo-id`.")

    if name_match_mode is not None and repo_name is None:
        raise typer.Exit("`repo-name` must be provided if `name-match-mode` is provided.")
    
    if repo_id is not None or repo_name is not None:
        from repoyard._models import get_repoyard_meta
        config = get_config(app_state['config_path'])
        repoyard_meta = get_repoyard_meta(config)
        
        if repo_id is not None:
            if not repo_id in repoyard_meta.by_id:
                raise typer.Exit(f"Repository with id `{repo_id}` not found.")
            repo_full_name = repoyard_meta.by_id[repo_id].full_name
        else:
            if name_match_mode is None: name_match_mode = NameMatchMode.SUBSEQUENCE
            if name_match_mode == NameMatchMode.EXACT:
                cmp = lambda x: x.name == repo_name if not name_match_case else x.name.lower() == repo_name.lower()
                repos_with_name = [x for x in repoyard_meta.by_full_name.values() if cmp(x)]
            elif name_match_mode == NameMatchMode.CONTAINS:
                cmp = lambda x: repo_name in x.name if not name_match_case else repo_name.lower() in x.name.lower()
                repos_with_name = [x for x in repoyard_meta.by_full_name.values() if cmp(x)]
            elif name_match_mode == NameMatchMode.SUBSEQUENCE:
                cmp = lambda x: _is_subsequence_match(repo_name, x.name) if not name_match_case else _is_subsequence_match(repo_name.lower(), x.name.lower())
                repos_with_name = [x for x in repoyard_meta.by_full_name.values() if cmp(x)]
            repos_with_name = sorted(repos_with_name, key=lambda x: x.full_name)
                
            if len(repos_with_name) == 0:
                raise typer.Exit(f"Repository with name `{repo_name}` not found.")
            elif len(repos_with_name) == 1:
                repo_full_name = repos_with_name[0].full_name
            else:
                from repoyard._utils import run_fzf
                _, repo_full_name = run_fzf(
                    terms=[r.full_name for r in repos_with_name],
                    disp_terms=[f"{r.name} ({r.repo_id})" for r in repos_with_name],
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
    storage_location: str|None = Option(None, "--storage-location", "-s", help="The storage location to create the new repository in."),
    repo_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, the id or the path of the repo."),
    from_path: Path|None = Option(None, "--from", "-f", help="Path to a local directory to move into repoyard as a new repository."),
    copy_from_path: bool = Option(False, "--copy", "-c", help="Copy the contents of the from_path into the new repository."),
    creator_hostname: str|None = Option(None, "--creator-hostname", help="Used to explicitly set the creator hostname of the new repository."),
    groups: list[str]|None = Option(None, "--groups", "-g", help="The groups to add the new repository to."),
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
    
    repo_full_name = new_repo(
        config_path=app_state['config_path'],
        storage_location=storage_location,
        repo_name=repo_name,
        from_path=from_path,
        copy_from_path=copy_from_path,
        creator_hostname=creator_hostname,
        initialise_git=initialise_git,
        verbose=False,
    )
    typer.echo(repo_full_name)

    if groups:
        from repoyard.cmds import modify_repometa
        modify_repometa(
            config_path=app_state['config_path'],
            repo_full_name=repo_full_name,
            modifications={
                'groups': groups,
            }
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
    name_match_mode: NameMatchMode|None = Option(None, "--name-match-mode", "-m", help="The mode to use for matching the repository name."),
    name_match_case: bool = Option(False, "--name-match-case", "-c", help="Whether to match the repository name case-sensitively."),
    sync_direction: SyncDirection|None = Option(None, "--sync-direction", "-d", help="The direction of the sync. If not provided, the appropriate direction will be automatically determined based on the sync status. This mode is only available for the 'CAREFUL' sync setting."),
    sync_setting: SyncSetting = Option(SyncSetting.CAREFUL, "--sync-setting", "-s", help="The sync setting to use."),
    sync_choices: list[RepoPart]|None = Option(None, "--sync-choices", "-c", help="The parts of the repository to sync. If not provided, all parts will be synced. By default, all parts are synced."),
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
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
    )

    if sync_choices is None:
        sync_choices = [repo_part for repo_part in RepoPart]
    
    asyncio.run(sync_repo(
        config_path=app_state['config_path'],
        repo_full_name=repo_full_name,
        sync_direction=sync_direction,
        sync_setting=sync_setting,
        sync_choices=sync_choices,
        verbose=True,
        show_rclone_progress=show_rclone_progress,
    ))


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
    max_concurrent_rclone_ops: int|None = Option(None, "--max-concurrent", "-m", help="The maximum number of concurrent rclone operations. If not provided, the default specified in the config will be used."),
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

    asyncio.run(sync_repometas(
        config_path=app_state['config_path'],
        repo_full_names=repo_full_names,
        storage_locations=storage_locations,
        sync_setting=sync_setting,
        sync_direction=sync_direction,
        verbose=True,
        max_concurrent_rclone_ops=max_concurrent_rclone_ops,
    ))


# %% [markdown]
# # `add-to-group`

# %%
#|export
@app.command(name='add-to-group')
def cli_add_to_group(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
    name_match_mode: NameMatchMode|None = Option(None, "--name-match-mode", "-m", help="The mode to use for matching the repository name."),
    name_match_case: bool = Option(False, "--name-match-case", "-c", help="Whether to match the repository name case-sensitively."),
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
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
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
            asyncio.run(sync_repometas(
                config_path=app_state['config_path'],
                repo_full_names=[repo_full_name],
                sync_setting=sync_setting,
                sync_direction=SyncDirection.PUSH,
                verbose=True,
            ))


# %% [markdown]
# # `remove-from-group`

# %%
#|export
@app.command(name='remove-from-group')
def cli_remove_from_group(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
    name_match_mode: NameMatchMode|None = Option(None, "--name-match-mode", "-m", help="The mode to use for matching the repository name."),
    name_match_case: bool = Option(False, "--name-match-case", "-c", help="Whether to match the repository name case-sensitively."),
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
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
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
            asyncio.run(sync_repometas(
                config_path=app_state['config_path'],
                repo_full_names=[repo_full_name],
                sync_setting=sync_setting,
                sync_direction=SyncDirection.PUSH,
                verbose=True,
            ))


# %% [markdown]
# # `include`

# %%
#|export
@app.command(name='include')
def cli_include(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
    name_match_mode: NameMatchMode|None = Option(None, "--name-match-mode", "-m", help="The mode to use for matching the repository name."),
    name_match_case: bool = Option(False, "--name-match-case", "-c", help="Whether to match the repository name case-sensitively."),
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
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
    )
    
    repoyard_meta = get_repoyard_meta(get_config(app_state['config_path']))
    if repo_full_name not in repoyard_meta.by_full_name:
        raise typer.Exit(f"Repository with full name `{repo_full_name}` not found.", code=1)
    
    asyncio.run(include_repo(
        config_path=app_state['config_path'],
        repo_full_name=repo_full_name,
    ))


# %% [markdown]
# # `exclude`

# %%
#|export
@app.command(name='exclude')
def cli_exclude(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
    name_match_mode: NameMatchMode|None = Option(None, "--name-match-mode", "-m", help="The mode to use for matching the repository name."),
    name_match_case: bool = Option(False, "--name-match-case", "-c", help="Whether to match the repository name case-sensitively."),
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
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
    )
    
    repoyard_meta = get_repoyard_meta(get_config(app_state['config_path']))
    if repo_full_name not in repoyard_meta.by_full_name:
        raise typer.Exit(f"Repository with full name `{repo_full_name}` not found.", code=1)
    
    asyncio.run(exclude_repo(
        config_path=app_state['config_path'],
        repo_full_name=repo_full_name,
        skip_sync=skip_sync,
    ))


# %% [markdown]
# # `delete`

# %%
#|export
@app.command(name='delete')
def cli_delete(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
    name_match_mode: NameMatchMode|None = Option(None, "--name-match-mode", "-m", help="The mode to use for matching the repository name."),
    name_match_case: bool = Option(False, "--name-match-case", "-c", help="Whether to match the repository name case-sensitively."),
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
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
    )
    
    repoyard_meta = get_repoyard_meta(get_config(app_state['config_path']))
    if repo_full_name not in repoyard_meta.by_full_name:
        raise typer.Exit(f"Repository with full name `{repo_full_name}` not found.", code=1)
    
    asyncio.run(delete_repo(
        config_path=app_state['config_path'],
        repo_full_name=repo_full_name,
    ))


# %% [markdown]
# # `repo-status`

# %%
#|exporti
def _dict_to_hierarchical_text(data: dict, indents: int=0, lines: list[str]=[]) -> list[str]:
    for k, v in data.items():
        if isinstance(v, dict):
            _dict_to_hierarchical_text(v, indents+1, lines)
        else:
            lines.append(f"{' ' *4*indents}{k}: {v}")
    return lines


# %%
#|export
@app.command(name='repo-status')
def cli_repo_status(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="The name of the repository to sync."),
    name_match_mode: NameMatchMode|None = Option(None, "--name-match-mode", "-m", help="The mode to use for matching the repository name."),
    name_match_case: bool = Option(False, "--name-match-case", "-c", help="Whether to match the repository name case-sensitively."),
    output_format: Literal['text', 'json'] = Option('text', "--output-format", "-o", help="The format of the output."),
):
    """
    Get the sync status of a repository.
    """
    from repoyard.cmds import get_repo_sync_status
    from repoyard._models import get_repoyard_meta
    from pydantic import BaseModel
    import json
    
    repo_full_name = _get_full_repo_name(
        repo_name=repo_name,
        repo_id=repo_id,
        repo_full_name=repo_full_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
    )
    
    repoyard_meta = get_repoyard_meta(get_config(app_state['config_path']))
    if repo_full_name not in repoyard_meta.by_full_name:
        raise typer.Exit(f"Repository with full name `{repo_full_name}` not found.", code=1)
    
    sync_status = asyncio.run(get_repo_sync_status(
        config_path=app_state['config_path'],
        repo_full_name=repo_full_name,
    ))

    data = {}
    for repo_part, part_sync_status in sync_status.items():
        part_sync_status_dump = part_sync_status._asdict()
        for k, v in part_sync_status_dump.items():
            if isinstance(v, BaseModel):
                part_sync_status_dump[k] = v.model_dump()
            if isinstance(v, Enum):
                part_sync_status_dump[k] = v.value
        data[repo_part.value] = part_sync_status_dump

    if output_format == 'json':
        typer.echo(json.dumps(data, indent=2))
    else:
       typer.echo("\n".join(_dict_to_hierarchical_text(data)))



# %% [markdown]
# # `yard-status`

# %% [markdown]
# # `path`

# %%
#|export
@app.command(name='path')
def cli_path(
    repo_full_name: str|None = Option(None, "--repo", "-r", help="The full name of the repository, in the form '{ULID}__{REPO_NAME}'."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
    repo_name: str|None = Option(None, "--repo-name", "-n", help="What repo path to show."),
    name_match_mode: NameMatchMode|None = Option(None, "--name-match-mode", "-m", help="The mode to use for matching the repository name."),
    name_match_case: bool = Option(False, "--name-match-case", "-c", help="Whether to match the repository name case-sensitively."),
    path_option: Literal[
            'data-user',
            'data',
            'meta',
            'conf',
            'root',
            'sync-record-data',
            'sync-record-meta',
            'sync-record-conf',

        ] = Option('data', "--path-option", "-p", help="The part of the repository to get the path of."),
):
    """
    Get the path of a repository.
    """
    from repoyard.cmds import get_repo_sync_status
    from repoyard._models import get_repoyard_meta
    from pydantic import BaseModel
    import json
    
    repo_full_name = _get_full_repo_name(
        repo_name=repo_name,
        repo_id=repo_id,
        repo_full_name=repo_full_name,
        name_match_mode=name_match_mode,
        name_match_case=name_match_case,
    )
    
    repoyard_meta = get_repoyard_meta(get_config(app_state['config_path']))
    if repo_full_name not in repoyard_meta.by_full_name:
        raise typer.Exit(f"Repository with full name `{repo_full_name}` not found.", code=1)
    repo_meta = repoyard_meta.by_full_name[repo_full_name]  

    config = get_config(app_state['config_path'])

    if path_option == 'data-user':
        typer.echo(repo_meta.get_user_path(config).as_posix())
    elif path_option == 'data':
        typer.echo(repo_meta.get_local_repodata_path(config).as_posix())
    elif path_option == 'meta':
        typer.echo(repo_meta.get_local_repometa_path(config).as_posix())
    elif path_option == 'conf':
        typer.echo(repo_meta.get_local_repoconf_path(config).as_posix())
    elif path_option == 'root':
        typer.echo(config.get_local_path(config).as_posix())
    elif path_option == 'sync-record-data':
        typer.echo(repo_meta.get_local_sync_record_path(config, RepoPart.DATA).as_posix())
    elif path_option == 'sync-record-meta':
        typer.echo(repo_meta.get_local_sync_record_path(config, RepoPart.META).as_posix())
    elif path_option == 'sync-record-conf':
        typer.echo(repo_meta.get_local_sync_record_path(config, RepoPart.CONF).as_posix())
    else:
        raise typer.Exit(f"Invalid path option: {path_option}", code=1)

# %% [markdown]
# # main

# %%
#|default_exp _cli.main

# %%
#|hide
import nblite; from nbdev.showdoc import show_doc; nblite.nbl_export()

# %%
#|export
import typer
from typer import Argument, Option
from typing_extensions import Annotated
from types import FunctionType
from typing import Callable, Union, List
from pathlib import Path

import repoyard as proj
from repoyard import const
from repoyard._cli.app import app


# %% [markdown]
# ## Main command

# %%
#|export
@app.callback()
def entrypoint(ctx: typer.Context):
    if ctx.invoked_subcommand is not None: return
    typer.echo(ctx.get_help())

# %%
# !repoyard

# %%
# @app.command(name='sync')
# def sync(
#     config_path: Path|None = None,
#     repo: str|None = Option(None, "--repo", "-r", help="The full name of the repository, the id or the path of the repo."),
#     repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
# ):
#     """
#     """
#     ...

# %%
# @app.command(name='init')
# def init(
#     config_path: Path|None = Option(None, "--config-path", help=f"The path to the config file. Will be {const.DEFAULT_CONFIG_PATH} if not provided."),
#     data_path: Path|None = Option(None, "--data-path", help=f"The path to the data directory. Will be {const.DEFAULT_DATA_PATH} if not provided."),
# ):
#     """
#     Initialize repoyard.
    
#     Will create the necessary folders and files to start using repoyard.
#     """
#     ...

# %%
# #|set_func_signature
# @app.command(name='new')
# def new(
#     config_path: Path|None = None,
#     storage_location: str|None = Option(None, "--location", "-l", help="The storage location to use for the new repository."),
#     repo_name: str|None = Option(None, "--name", "-n", help="The name of the new repository."),
#     from_path: Path|None = Option(None, "--from", "-f", help="Path to a local directory to move into repoyard as a new repository."),
#     creator_hostname: str|None = Option(None, "--creator-hostname", "-c", help="Used to explicitly set the creator hostname of the new repository."),
# ):
#     """
#     """
#     ...

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

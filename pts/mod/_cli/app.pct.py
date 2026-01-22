# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # app

# %%
# |default_exp _cli.app

# %%
# |hide
from nblite import nbl_export

nbl_export()

# %%
# |export
import typer

# %%
# |export
app = typer.Typer(invoke_without_command=True)
app_state = {}

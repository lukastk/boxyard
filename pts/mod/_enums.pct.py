# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _enums
#
# Lightweight enum definitions used by the CLI layer.
# Kept separate from heavy modules so typer can import them without pulling in pydantic, asyncio, etc.

# %%
#|default_exp _enums

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
from enum import Enum


class BoxPart(str, Enum):
    DATA = "data"
    META = "meta"
    CONF = "conf"


class SyncSetting(str, Enum):
    CAREFUL = "careful"
    REPLACE = "replace"
    FORCE = "force"


class SyncDirection(str, Enum):
    PUSH = "push"  # local -> remote
    PULL = "pull"  # remote -> local


class RenameScope(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
    BOTH = "both"


class SyncNameDirection(str, Enum):
    TO_LOCAL = "to_local"
    TO_REMOTE = "to_remote"

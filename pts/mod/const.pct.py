# %% [markdown]
# # const

# %%
#|default_exp const

# %%
#|hide
import nblite; from nbdev.showdoc import show_doc; nblite.nbl_export()

# %%
#|export
from pathlib import Path
import inspect
from pydantic import BaseModel, ConfigDict
import repoyard as proj

# %%
#|export
pkg_path = Path(proj.__file__).parent

# %% [markdown]
# Default paths

# %%
#|export
DEFAULT_CONFIG_PATH = Path("~") / ".config" / "repoyard" / "config.json"
DEFAULT_DATA_PATH = Path("~") / ".repoyard"
DEFAULT_USER_REPOS_PATH = Path("~") / "repos"
DEFAULT_USER_REPO_GROUPS_PATH = Path("~") / "repo_groups"

LOCAL_REPOMETA_REL_PATH = "local/repometa"
LOCAL_REPOSTORE_REL_PATH = "local/repostore"

# %%
#|export
DEFAULT_REPOYARD_IGNORE = inspect.cleandoc("""
.venv/
node_modules/
__pycache__/
""")

# %% [markdown]
# Environment variables

# %%
#|export
ENV_VAR_REPOYARD_CONFIG_PATH = "REPOYARD_CONFIG_PATH"


# %% [markdown]
# Misc

# %%
#|export
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

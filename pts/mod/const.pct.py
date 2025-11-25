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
import string
from pydantic import BaseModel, ConfigDict
import repoyard as proj

# %%
#|export
pkg_path = Path(proj.__file__).parent

# %% [markdown]
# Default paths

# %%
#|export
DEFAULT_CONFIG_PATH = Path("~") / ".config" / "repoyard" / "config.toml"
DEFAULT_DATA_PATH = Path("~") / ".repoyard"
DEFAULT_USER_REPOS_PATH = Path("~") / "repos"
DEFAULT_USER_REPO_GROUPS_PATH = Path("~") / "repo-groups"

SYNC_RECORDS_REL_PATH = "sync_records"
REMOTE_REPOS_REL_PATH = "repos"
REMOTE_BACKUP_REL_PATH = "sync_backups"

REPO_DATA_REL_PATH = "data"
REPO_METAFILE_REL_PATH = "repometa.toml"
REPO_CONF_REL_PATH = "conf"

SOFT_INTERRUPT_COUNT = 3

DEFAULT_FAKE_STORE_REL_PATH = "fake_store"

# %% [markdown]
# Other constants

# %%
#|export
DEFAULT_RCLONE_EXCLUDE = inspect.cleandoc("""
.venv/
.pixi/
.trunk/
node_modules/
__pycache__/

.DS_Store
""")

REPO_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
REPO_TIMESTAMP_FORMAT_DATE_ONLY = "%Y%m%d"
DEFAULT_REPO_SUBID_CHARACTER_SET = string.ascii_lowercase + string.ascii_uppercase + string.digits
DEFAULT_REPO_SUBID_LENGTH = 5

DEFAULT_MAX_CONCURRENT_RCLONE_OPS = 3

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

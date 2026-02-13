# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # const

# %%
#|default_exp const

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
from pathlib import Path
import textwrap
import string
from pydantic import BaseModel, ConfigDict
import boxyard as proj

# %%
#|export
pkg_path = Path(proj.__file__).parent

# %% [markdown]
# Default paths

# %%
#|export
DEFAULT_CONFIG_PATH = Path("~") / ".config" / "boxyard" / "config.toml"
DEFAULT_DATA_PATH = Path("~") / ".boxyard"
DEFAULT_USER_BOXES_PATH = Path("~") / "boxes"
DEFAULT_USER_BOX_GROUPS_PATH = Path("~") / "box-groups"

SYNC_RECORDS_REL_PATH = "sync_records"
REMOTE_BOXES_REL_PATH = "boxes"
REMOTE_BACKUP_REL_PATH = "sync_backups"

BOX_DATA_REL_PATH = "data"
BOX_METAFILE_REL_PATH = "boxmeta.toml"
BOX_CONF_REL_PATH = "conf"

SOFT_INTERRUPT_COUNT = 3

DEFAULT_FAKE_STORE_REL_PATH = "fake_store"

# %% [markdown]
# Other constants

# %%
#|export
DEFAULT_RCLONE_EXCLUDE = textwrap.dedent("""
.venv/
.pixi/
.trunk/
node_modules/
__pycache__/

.DS_Store
""").strip()

BOX_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
BOX_TIMESTAMP_FORMAT_DATE_ONLY = "%Y%m%d"
DEFAULT_BOX_SUBID_CHARACTER_SET = string.ascii_lowercase + string.digits
DEFAULT_BOX_SUBID_LENGTH = 5

DEFAULT_MAX_CONCURRENT_RCLONE_OPS = 3

# %%
subid_num = len(DEFAULT_BOX_SUBID_CHARACTER_SET) ** DEFAULT_BOX_SUBID_LENGTH
print(f"Number of possible subids: {subid_num / 1e6} million.\n")

p_no_collide = 1 - (1 / subid_num)
for i in range(2, 7):
    print(
        f"Likelihood of collision if creating 1e{i} boxes with the same name per day:"
    )
    num = 10**i
    print(f"  {1 - p_no_collide**num:.2e}")

# %% [markdown]
# Environment variables

# %%
#|export
ENV_VAR_BOXYARD_CONFIG_PATH = "BOXYARD_CONFIG_PATH"

# %% [markdown]
# Misc

# %%
#|export
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

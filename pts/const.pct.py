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
import repoyard as proj

# %%
#|export
pkg_path = Path(proj.__file__).parent

# %% [markdown]
# Default paths

# %%
#|export
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "repoyard" / "config.json"

# %% [markdown]
# Environment variables

# %%
#|export
ENV_VAR_REPOYARD_CONFIG_PATH = "REPOYARD_CONFIG_PATH"

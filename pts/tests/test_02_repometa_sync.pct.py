# %% [markdown]
# # test_01_cli

# %%
#|default_exp test_01_cli
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import tests as this_module

# %%
#|top_export
import subprocess
from pathlib import Path
import shutil
import toml
from repoyard import const


# %%
#|set_func_signature
def test_01_cli(): ...

# %%
#|export

# %% [markdown]
# # test_00_sync

# %%
#|default_exp test_00_sync
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
def test_00_sync(): ...


# %%
#|export
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = tests_working_dir / "test_00_sync"
if test_folder_path.exists():
    shutil.rmtree(test_folder_path)
test_folder_path.mkdir(parents=True, exist_ok=True)

# %%
home_path = test_folder_path / "home"
config_path = home_path / ".config" / "repoyard" / "config.toml"
data_path = home_path / ".repoyard"

storage_loc_path = test_folder_path / "storage_location"

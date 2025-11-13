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

# %% [markdown]
# # Run `init`

# %%
#|export
subprocess.run([
    "repoyard", "init",
    "--config-path", config_path,
    "--data-path", data_path,
]);

# %% [markdown]
# Modify the config

# %%
#|export
config = toml.load(config_path)
config['user_repos_path'] = home_path / "user_repos"
config['user_repo_groups_path'] = home_path / "user_repo_groups"
with open(config_path, "w") as f: toml.dump(config, f)

# %% [markdown]
# Add a local storage location

# %%
(config_path.parent / "repoyard_rclone.conf").write_text(
f"""
[test_local]
type = alias
remote = {storage_loc_path}
"""
)
storage_loc_path.mkdir(parents=True, exist_ok=True)

# %% [markdown]
# # Create new repo using `new` and sync it to the storage location

# %%

# %% [markdown]
# # Make edit inside the storage location and sync it back to the local copy

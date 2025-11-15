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
from repoyard.cmds import *
from repoyard._models import get_repoyard_meta
from repoyard.config import get_config


# %%
#|set_func_signature
def test_00_sync(): ...


# %% [markdown]
# Parameters

# %%
#|export
num_test_repos = 20

# %% [markdown]
# # Initialise `init_repoyard`

# %%
#|export
# Set up test folders
import tempfile
test_folder_path = Path(tempfile.mkdtemp(prefix="sync_repo", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
config_path = test_folder_path / ".config" / "repoyard" / "config.toml"
data_path = test_folder_path / ".repoyard"

# Run init
init_repoyard(config_path=config_path, data_path=data_path)

# Add a storage location 'my_remote'
import toml
test_storage_location_name = "my_remote"
config_dump = toml.load(config_path)
remote_rclone_path = Path(tempfile.mkdtemp(prefix="rclone_remote", dir="/tmp"))
config_dump['storage_locations'][test_storage_location_name] = {
    'storage_type' : "rclone",
    'store_path' : "repoyard",
}
config_path.write_text(toml.dumps(config_dump))

# Load config
config = get_config(config_path)

# Set up a rclone remote path for testing
config.rclone_config_path.write_text(f"""
[{test_storage_location_name}]
type = alias
remote = {remote_rclone_path}
""");

# %% [markdown]
# # Create some repos using `new_repo` and sync them using `sync_repo`

# %%
#|export
repo_full_names = []
for i in range(num_test_repos):
    repo_full_name = new_repo(config_path=config_path, repo_name=f"test_repo_{i}", storage_location=test_storage_location_name)
    repo_full_names.append(repo_full_name)
    
# Verify that the repos are included
repoyard_meta = get_repoyard_meta(config, force_create=True)
for repo_full_name in repo_full_names:
    assert repoyard_meta.by_full_name[repo_full_name].check_included(config)

# %% [markdown]
# # Exclude all repos using `exclude_repo`

# %%
#|export
for repo_full_name in repo_full_names:
    exclude_repo(config_path=config_path, repo_full_name=repo_full_name)
    
# Verify that the repos have been excluded
repoyard_meta = get_repoyard_meta(config, force_create=True)
for repo_full_name in repo_full_names:
    assert not repoyard_meta.by_full_name[repo_full_name].check_included(config)

# %% [markdown]
# # Include all repos using `include_repo`

# %%
#|export
for repo_full_name in repo_full_names:
    include_repo(config_path=config_path, repo_full_name=repo_full_name)
    
# Verify that the repos are included
repoyard_meta = get_repoyard_meta(config, force_create=True)
for repo_full_name in repo_full_names:
    assert repoyard_meta.by_full_name[repo_full_name].check_included(config)

# %% [markdown]
# # Delete all repos using `delete_repo`

# %%
#|export
for repo_full_name in repo_full_names:
    delete_repo(config_path=config_path, repo_full_name=repo_full_name)

# Verify that the repos have been deleted
for repo_meta in repoyard_meta.by_full_name.values():
    assert not repo_meta.get_local_path(config).exists()
    assert not (remote_rclone_path / repo_meta.get_remote_path(config)).exists()

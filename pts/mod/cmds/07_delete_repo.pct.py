# %% [markdown]
# # _delete_repo

# %%
#|default_exp cmds._delete_repo
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path

from repoyard._utils.bisync_helper import SyncSetting
from repoyard.config import get_config
from repoyard import const


# %%
#|set_func_signature
def delete_repo(
    config_path: Path,
    repo_full_name: str,
):
    """
    """
    ...


# %% [markdown]
# Set up testing args

# %%
# Set up test environment
import tempfile
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = Path(tempfile.mkdtemp(prefix="delete_repo", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
symlink_path = tests_working_dir / "_cmds" / "delete_repo"
symlink_path.parent.mkdir(parents=True, exist_ok=True)
if symlink_path.exists() or symlink_path.is_symlink():
    symlink_path.unlink()
symlink_path.symlink_to(test_folder_path, target_is_directory=True) # So that it can be viewed from within the project working directory
data_path = test_folder_path / ".repoyard"

# %%
# Args (1/2)
config_path = test_folder_path / "repoyard_config" / "config.toml"
sync_force = False
skip_sync = True

# %%
# Run init
from repoyard.cmds import init_repoyard, new_repo, sync_repo
init_repoyard(config_path=config_path, data_path=data_path)

# Add a storage location 'my_remote'
import toml
config_dump = toml.load(config_path)
remote_rclone_path = Path(tempfile.mkdtemp(prefix="rclone_remote", dir="/tmp"))
config_dump['storage_locations']['my_remote'] = {
    'storage_type' : "rclone",
    'store_path' : "repoyard",
}
config_path.write_text(toml.dumps(config_dump))

# Args (2/2)
repo_full_name = new_repo(config_path=config_path, repo_name="test_repo", storage_location="my_remote")

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

# %%
# Set up a rclone remote path for testing
config.rclone_config_path.write_text(f"""
[my_remote]
type = alias
remote = {remote_rclone_path}
""");

sync_repo(config_path=config_path, repo_full_name=repo_full_name)

# %% [markdown]
# Ensure that repo exists

# %%
#|export
from repoyard._repos import get_repoyard_meta
repoyard_meta = get_repoyard_meta(config)

if repo_full_name not in repoyard_meta.by_full_name:
    raise ValueError(f"Repo '{repo_full_name}' does not exist.")

repo_meta = repoyard_meta.by_full_name[repo_full_name]

# %%
assert repo_meta.get_local_path(config).exists()
assert (remote_rclone_path / repo_meta.get_remote_path(config)).exists()

# %%
#|export

# Delete local repo
import shutil
shutil.rmtree(repo_meta.get_local_path(config))

# Delete remote repo
from repoyard._utils import rclone_purge
from repoyard.config import StorageType
if repo_meta.get_storage_location_config(config).storage_type != StorageType.LOCAL:
    rclone_purge(
        config.rclone_config_path,
        source=repo_meta.storage_location,
        source_path=repo_meta.get_remote_path(config),
    )

# %%
assert not repo_meta.get_local_path(config).exists()
assert not (remote_rclone_path / repo_meta.get_remote_path(config)).exists()

# %% [markdown]
# Refresh the repoyard meta file

# %%
#|export
from repoyard._repos import refresh_repoyard_meta
refresh_repoyard_meta(config)

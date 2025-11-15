# %% [markdown]
# # _include_repo

# %%
#|default_exp cmds._include_repo
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path

from repoyard.config import get_config
from repoyard import const


# %%
#|set_func_signature
async def include_repo(
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
test_folder_path = Path(tempfile.mkdtemp(prefix="include_repo", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
symlink_path = tests_working_dir / "_cmds" / "include_repo"
symlink_path.parent.mkdir(parents=True, exist_ok=True)
if symlink_path.exists() or symlink_path.is_symlink():
    symlink_path.unlink()
symlink_path.symlink_to(test_folder_path, target_is_directory=True) # So that it can be viewed from within the project working directory
data_path = test_folder_path / ".repoyard"

# %%
# Args (1/2)
config_path = test_folder_path / "repoyard_config" / "config.toml"

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

await sync_repo(config_path=config_path, repo_full_name=repo_full_name)
# Remove the repo from the local store to test the inclusion
from repoyard.cmds import exclude_repo
await exclude_repo(config_path=config_path, repo_full_name=repo_full_name)

# %% [markdown]
# Check if repo is already included

# %%
#|export
from repoyard._models import get_repoyard_meta
repoyard_meta = get_repoyard_meta(config)

if repo_full_name not in repoyard_meta.by_full_name:
    raise ValueError(f"Repo '{repo_full_name}' does not exist.")

repo_meta = repoyard_meta.by_full_name[repo_full_name]

if repo_meta.check_included(config):
    raise ValueError(f"Repo '{repo_full_name}' is already included.")

# %% [markdown]
# Include it

# %%
#|export
from repoyard.cmds import sync_repo
from repoyard._models import RepoPart
from repoyard._utils.sync_helper import sync_helper, SyncSetting, SyncDirection

# First force sync the data
await sync_repo(
    config_path=config_path,
    repo_full_name=repo_full_name,
    sync_direction=SyncDirection.PULL,
    sync_setting=SyncSetting.FORCE,
    sync_choices=[RepoPart.DATA],
)

# Then sync the rest
await sync_repo(
    config_path=config_path,
    repo_full_name=repo_full_name,
    sync_direction=None,
    sync_setting=SyncSetting.CAREFUL,
    sync_choices=[RepoPart.META, RepoPart.CONF],
);

# %%
# Should now be included
assert repo_meta.check_included(config)

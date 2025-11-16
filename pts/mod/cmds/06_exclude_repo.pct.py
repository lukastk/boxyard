# %% [markdown]
# # _exclude_repo

# %%
#|default_exp cmds._exclude_repo
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path

from repoyard._utils.sync_helper import SyncSetting
from repoyard.config import get_config
from repoyard import const


# %%
#|set_func_signature
async def exclude_repo(
    config_path: Path,
    repo_full_name: str,
    skip_sync: bool = False,
    soft_interruption_enabled: bool = True,
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
test_folder_path = Path(tempfile.mkdtemp(prefix="exclude_repo", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
symlink_path = tests_working_dir / "_cmds" / "exclude_repo"
symlink_path.parent.mkdir(parents=True, exist_ok=True)
if symlink_path.exists() or symlink_path.is_symlink():
    symlink_path.unlink()
symlink_path.symlink_to(test_folder_path, target_is_directory=True) # So that it can be viewed from within the project working directory
data_path = test_folder_path / ".repoyard"

# %%
# Args (1/2)
config_path = test_folder_path / "repoyard_config" / "config.toml"
skip_sync = True
soft_interruption_enabled = True

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

await sync_repo(config_path=config_path, repo_full_name=repo_full_name, soft_interruption_enabled=soft_interruption_enabled);

# %% [markdown]
# Ensure that repo is included

# %%
#|export
from repoyard._models import get_repoyard_meta
repoyard_meta = get_repoyard_meta(config)

if repo_full_name not in repoyard_meta.by_full_name:
    raise ValueError(f"Repo '{repo_full_name}' does not exist.")

repo_meta = repoyard_meta.by_full_name[repo_full_name]

if not repo_meta.check_included(config):
    raise ValueError(f"Repo '{repo_full_name}' is already excluded.")

# %% [markdown]
# Sync any changes before removing locally

# %%
#|export
from repoyard.cmds import sync_repo

if not skip_sync:
    await sync_repo(
        config_path=config_path,
        repo_full_name=repo_full_name,
        sync_setting=SyncSetting.CAREFUL,
    )

# %% [markdown]
# Exclude it

# %%
#|export
import shutil
from repoyard._models import RepoPart
shutil.rmtree(repo_meta.get_local_repodata_path(config))
repo_meta.get_local_sync_record_path(config, RepoPart.DATA).unlink()

# %%
# Should now be included
assert not repo_meta.check_included(config)

# %% [markdown]
# # _sync_repometas

# %%
#|default_exp cmds._sync_repometas
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path

from repoyard._utils.bisync_helper import bisync_helper, SyncSetting
from repoyard.config import get_config, StorageType
from repoyard import const


# %%
#|set_func_signature
def sync_repometas(
    config_path: Path,
    repo_full_names: list[str]|None = None,
    storage_locations: list[str]|None = None,
    sync_setting: SyncSetting = SyncSetting.BISYNC,
    force: bool = False,
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
test_folder_path = Path(tempfile.mkdtemp(prefix="sync_repometas", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
symlink_path = tests_working_dir / "_cmds" / "sync_repometas"
symlink_path.parent.mkdir(parents=True, exist_ok=True)
if symlink_path.exists() or symlink_path.is_symlink():
    symlink_path.unlink()
symlink_path.symlink_to(test_folder_path, target_is_directory=True) # So that it can be viewed from within the project working directory
data_path = test_folder_path / ".repoyard"

# %%
# Args (1/2)
config_path = test_folder_path / "repoyard_config" / "config.toml"
repo_full_names = None
storage_locations = None
sync_setting = SyncSetting.BISYNC
force = False

# %%
# Run init
from repoyard.cmds import init_repoyard
from repoyard.cmds import new_repo
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

new_repo(config_path=config_path, repo_name="test_repo1", storage_location="my_remote")
new_repo(config_path=config_path, repo_name="test_repo2", storage_location="my_remote")
new_repo(config_path=config_path, repo_name="test_repo3", storage_location="my_remote")

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

if repo_full_names is not None and storage_locations is not None:
    raise ValueError("Cannot provide both `repo_full_names` and `storage_locations`.")

# %%
# Set up a rclone remote path for testing
config.rclone_config_path.write_text(f"""
[my_remote]
type = alias
remote = {remote_rclone_path}
""");

# %% [markdown]
# Sync

# %%
#|export

# Sync by storage location
if repo_full_names is None:
    # Get all paths to all storage locations to sync
    storage_locations_to_sync = [sl_name for sl_name in config.storage_locations if config.storage_locations[sl_name].storage_type != StorageType.LOCAL]
    if storage_locations is not None:
        storage_locations_to_sync = [sl_name for sl_name in storage_locations_to_sync if sl_name in storage_locations]
        
    # Sync each storage location
    for sl_name in storage_locations_to_sync:
        local_path = config.local_store_path / sl_name
        remote_path = config.storage_locations[sl_name].store_path
        bisync_helper(
            rclone_config_path=config.rclone_config_path,
            sync_setting=sync_setting,
            local_path=local_path,
            remote=sl_name,
            remote_path=remote_path,
            force=force,
            include=[f"/*/{const.REPO_METAFILE_REL_PATH}"]
        )
            
# Sync by repo name     
else:
    from repoyard._repos import get_repoyard_meta
    repoyard_meta = get_repoyard_meta(config)
    
    # Ensure all repo names are present in repoyard_meta
    for repo_full_name in repo_full_names:
        if repo_full_name not in repoyard_meta.by_full_name:
            raise ValueError(f"Repo '{repo_full_name}' not found.")
        
    # Sync each storage location
    for sl_name in config.storage_locations:
        if config.storage_locations[sl_name].storage_type == StorageType.LOCAL: continue
        sl_repo_full_names = [repo_full_name for repo_full_name in repo_full_names if repoyard_meta.by_full_name[repo_full_name].storage_location == sl_name]
        local_path = config.local_store_path / sl_name
        remote_path = config.storage_locations[sl_name].store_path
        if len(sl_repo_full_names) == 0: continue
        includes = [
            f"/{repo_full_name}/{const.REPO_METAFILE_REL_PATH}"
            for repo_full_name in sl_repo_full_names
        ]
        bisync_helper(
            rclone_config_path=config.rclone_config_path,
            sync_setting=sync_setting,
            local_path=local_path,
            remote=sl_name,
            remote_path=remote_path,
            force=force,
            include=includes,
        )

# %% [markdown]
# Refresh the repoyard meta file

# %%
#|export
from repoyard._repos import refresh_repoyard_meta
refresh_repoyard_meta(config)

# %% [markdown]
# Check that the sync worked

# %%
# Check that the synced worked
from repoyard._utils import rclone_lsjson
sl_name = "my_remote"
_repo_full_names = [p.name for p in (data_path / "local_store" / "my_remote").glob("*")]

for _repo_full_name in _repo_full_names:
    _lsjson = rclone_lsjson(
        rclone_config_path=config.rclone_config_path,
        source=sl_name,
        source_path=config.storage_locations[sl_name].store_path / _repo_full_name
    )
    assert const.REPO_METAFILE_REL_PATH in {f["Name"] for f in _lsjson}

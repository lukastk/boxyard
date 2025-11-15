# %% [markdown]
# # _sync_repo

# %%
#|default_exp cmds._sync_repo
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path
from enum import Enum

from repoyard._utils.sync_helper import sync_helper, SyncSetting, SyncDirection
from repoyard._models import SyncStatus, RepoPart
from repoyard.config import get_config, StorageType
from repoyard import const


# %%
#|set_func_signature
async def sync_repo(
    config_path: Path,
    repo_full_name: str,
    sync_direction: SyncDirection|None = None,
    sync_setting: SyncSetting = SyncSetting.CAREFUL,
    sync_choices: list[RepoPart]|None = None,
    verbose: bool = False,
    show_rclone_progress: bool = False,
) -> dict[RepoPart, SyncStatus]:
    """
    Syncs a repo with its remote.
    
    Args:
        config_path: Path to the repoyard config file.
        repo_full_name: Full name of the repository to sync.
        sync_direction: Direction of sync.
        sync_setting: SyncSetting option (SAFE, CAREFUL, FORCE).
        sync_choices: List of RepoPart specifying what to sync. If None, all parts are synced.
        force: Force syncing, possibly overwriting changes.
        verbose: Print verbose output during sync.
        show_rclone_progress: Show rclone progress during sync.
    """
    ...


# %% [markdown]
# Set up testing args

# %%
# Set up test environment
import tempfile
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = Path(tempfile.mkdtemp(prefix="sync_repo", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
symlink_path = tests_working_dir / "_cmds" / "sync_repo"
symlink_path.parent.mkdir(parents=True, exist_ok=True)
if symlink_path.exists() or symlink_path.is_symlink():
    symlink_path.unlink()
symlink_path.symlink_to(test_folder_path, target_is_directory=True) # So that it can be viewed from within the project working directory
data_path = test_folder_path / ".repoyard"

# %%
# Args (1/2)
config_path = test_folder_path / "repoyard_config" / "config.toml"
sync_direction = None
sync_setting = SyncSetting.CAREFUL
sync_choices = None
verbose = True
show_rclone_progress = False

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

# Args (2/2)
repo_full_name = new_repo(config_path=config_path, repo_name="test_repo", storage_location="my_remote")

# %%
# Put an excluded file into the repo data folder to make sure it is not synced
(data_path / "local_store" / "my_remote" / repo_full_name / const.REPO_DATA_REL_PATH / ".venv").mkdir(parents=True, exist_ok=True)
(data_path / "local_store" / "my_remote" / repo_full_name / const.REPO_DATA_REL_PATH / ".venv" / "test.txt").write_text("test");

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)
if sync_choices is None:
    sync_choices = [repo_part for repo_part in RepoPart]

# %%
# Set up a rclone remote path for testing
config.rclone_config_path.write_text(f"""
[my_remote]
type = alias
remote = {remote_rclone_path}
""");

# %% [markdown]
# Find the repo meta

# %%
#|export
from repoyard._models import get_repoyard_meta
repoyard_meta = get_repoyard_meta(config)

if repo_full_name not in repoyard_meta.by_full_name:
    raise ValueError(f"Repo '{repo_full_name}' not found.")

repo_meta = repoyard_meta.by_full_name[repo_full_name]

# %% [markdown]
# Check if the repo is in a local storage location, in which case quit.

# %%
#|export
if repo_meta.get_storage_location_config(config).storage_type == StorageType.LOCAL:
    pass
    #|return_line

# %% [markdown]
# Prints

# %%
#|export
if verbose:
    print(f"Syncing repo {repo_full_name} at {repo_meta.storage_location}.")

# %% [markdown]
# Sync the repometa

# %%
#|export
sync_results = {}

sync_part = RepoPart.META
if sync_part in sync_choices:
    if verbose: print(f"Syncing {sync_part.value}.")
    sync_results[RepoPart.META] = await sync_helper(
        rclone_config_path=config.rclone_config_path,
        sync_direction=sync_direction,
        sync_setting=sync_setting,
        local_path=repo_meta.get_local_repometa_path(config),
        local_sync_record_path=repo_meta.get_local_sync_record_path(config, sync_part),
        remote=repo_meta.storage_location,
        remote_path=repo_meta.get_remote_repometa_path(config),
        remote_sync_record_path=repo_meta.get_remote_sync_record_path(config, sync_part),
        verbose=verbose,
        show_rclone_progress=show_rclone_progress,
    )

# %%
# Check that the synced worked
from repoyard._utils import rclone_lsjson
_lsjson = await rclone_lsjson(
    rclone_config_path=config.rclone_config_path,
    source=repo_meta.storage_location,
    source_path=repo_meta.get_remote_path(config)
)
assert "repometa.toml" in {f["Name"] for f in _lsjson}

# %% [markdown]
# Sync the repoconf

# %%
#|export
sync_part = RepoPart.CONF
if sync_part in sync_choices:
    if verbose: print("Syncing", sync_part.value)
    sync_results[sync_part] = await sync_helper(
        rclone_config_path=config.rclone_config_path,
        sync_direction=sync_direction,
        sync_setting=sync_setting,
        local_path=repo_meta.get_local_repoconf_path(config),
        local_sync_record_path=repo_meta.get_local_sync_record_path(config, sync_part),
        remote=repo_meta.storage_location,
        remote_path=repo_meta.get_remote_repoconf_path(config),
        remote_sync_record_path=repo_meta.get_remote_sync_record_path(config, sync_part),
        verbose=verbose,
        show_rclone_progress=show_rclone_progress,
    )

# %%
# Check that the synced worked
from repoyard._utils import rclone_lsjson
_lsjson = await rclone_lsjson(
    rclone_config_path=config.rclone_config_path,
    source=repo_meta.storage_location,
    source_path=repo_meta.get_remote_repoconf_path(config)
)
assert ".repoyard_exclude" in {f["Name"] for f in _lsjson}

# %% [markdown]
# Get the now locally synced conf files for the sync of the repo data

# %%
#|export
_repoyard_include_path = repo_meta.get_local_repoconf_path(config) / ".repoyard_include"
_repoyard_exclude_path = repo_meta.get_local_repoconf_path(config) / ".repoyard_exclude"
_repoyard_filters_path = repo_meta.get_local_repoconf_path(config) / ".repoyard_filters"

_repoyard_include_path = _repoyard_include_path if _repoyard_include_path.exists() else None
_repoyard_exclude_path = _repoyard_exclude_path if _repoyard_exclude_path.exists() else None
_repoyard_filters_path = _repoyard_filters_path if _repoyard_filters_path.exists() else None

# %% [markdown]
# Sync the repo data

# %%
#|export
sync_part = RepoPart.DATA
if sync_part in sync_choices:
    if verbose: print("Syncing", sync_part.value)
    sync_results[sync_part] = await sync_helper(
        rclone_config_path=config.rclone_config_path,
        sync_direction=sync_direction,
        sync_setting=sync_setting,
        local_path=repo_meta.get_local_repodata_path(config),
        local_sync_record_path=repo_meta.get_local_sync_record_path(config, sync_part),
        remote=repo_meta.storage_location,
        remote_path=repo_meta.get_remote_repodata_path(config),
        remote_sync_record_path=repo_meta.get_remote_sync_record_path(config, sync_part),
        include_path=_repoyard_include_path,
        exclude_path=_repoyard_exclude_path,
        filters_path=_repoyard_filters_path,
        verbose=verbose,
        show_rclone_progress=show_rclone_progress,
    )

# %%
# Check that the synced worked
from repoyard._utils import rclone_lsjson
_lsjson = await rclone_lsjson(
    rclone_config_path=config.rclone_config_path,
    source=repo_meta.storage_location,
    source_path=repo_meta.get_remote_repodata_path(config)
)
assert ".git" in {f["Name"] for f in _lsjson}
assert ".venv" not in {f["Name"] for f in _lsjson}

# %% [markdown]
# Refresh the repoyard meta file

# %%
#|export
if RepoPart.META in sync_choices:
    from repoyard._models import refresh_repoyard_meta
    refresh_repoyard_meta(config)

# %%
#|func_return
sync_results;

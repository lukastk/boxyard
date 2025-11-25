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
from repoyard._utils import async_throttler
from repoyard._utils import check_interrupted, enable_soft_interruption, SoftInterruption
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
    soft_interruption_enabled: bool = True,
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
from tests.utils import *
remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %%
# Args (1/2)
from repoyard.cmds import new_repo
config_path = config_path
repo_full_name = new_repo(config_path=config_path, repo_name="test_repo", storage_location="my_remote")
sync_direction = None
sync_setting = SyncSetting.CAREFUL
sync_choices = None
verbose = True
show_rclone_progress = False
soft_interruption_enabled = True

# %%
# Put an excluded file into the repo data folder to make sure it is not synced
(data_path / "local_store" / "my_remote" / repo_full_name / "test_repo" / ".venv").mkdir(parents=True, exist_ok=True)
(data_path / "local_store" / "my_remote" / repo_full_name / "test_repo"/ ".venv" / "test.txt").write_text("test");

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)
if sync_choices is None:
    sync_choices = [repo_part for repo_part in RepoPart]

if soft_interruption_enabled:
    enable_soft_interruption()

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
# Get the backup locations

# %%
#|export
sl_config = repo_meta.get_storage_location_config(config)
local_sync_backups_path = config.local_sync_backups_path
remote_sync_backups_path = sl_config.store_path / const.REMOTE_BACKUP_REL_PATH

# %% [markdown]
# Sync the repometa

# %%
#|export
sync_results = {}

if check_interrupted(): raise SoftInterruption()

sync_part = RepoPart.META
if sync_part in sync_choices:
    if verbose: print(f"Syncing {sync_part.value}.")
    sync_results[RepoPart.META] = await sync_helper(
        rclone_config_path=config.rclone_config_path,
        sync_direction=sync_direction,
        sync_setting=sync_setting,
        local_path=repo_meta.get_local_part_path(config, RepoPart.META),
        local_sync_record_path=repo_meta.get_local_sync_record_path(config, sync_part),
        remote=repo_meta.storage_location,
        remote_path=repo_meta.get_remote_part_path(config, RepoPart.META),
        remote_sync_record_path=repo_meta.get_remote_sync_record_path(config, sync_part),
        local_sync_backups_path=local_sync_backups_path,
        remote_sync_backups_path=remote_sync_backups_path,
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
if check_interrupted(): raise SoftInterruption()

sync_part = RepoPart.CONF
if sync_part in sync_choices:
    if verbose: print("Syncing", sync_part.value)
    sync_results[sync_part] = await sync_helper(
        rclone_config_path=config.rclone_config_path,
        sync_direction=sync_direction,
        sync_setting=sync_setting,
        local_path=repo_meta.get_local_part_path(config, RepoPart.CONF),
        local_sync_record_path=repo_meta.get_local_sync_record_path(config, sync_part),
        remote=repo_meta.storage_location,
        remote_path=repo_meta.get_remote_part_path(config, RepoPart.CONF),
        remote_sync_record_path=repo_meta.get_remote_sync_record_path(config, sync_part),
        local_sync_backups_path=local_sync_backups_path,
        remote_sync_backups_path=remote_sync_backups_path,
        verbose=verbose,
        show_rclone_progress=show_rclone_progress,
    )

# %%
# Check that the synced worked
from repoyard._utils import rclone_lsjson
_lsjson = await rclone_lsjson(
    rclone_config_path=config.rclone_config_path,
    source=repo_meta.storage_location,
    source_path=repo_meta.get_remote_part_path(config, RepoPart.CONF)
)
assert _lsjson is None # Empty by default

# %% [markdown]
# Get the now locally synced conf files for the sync of the repo data

# %%
#|export
_rclone_include_path = repo_meta.get_local_part_path(config, RepoPart.CONF) / ".rclone_include"
_rclone_exclude_path = repo_meta.get_local_part_path(config, RepoPart.CONF) / ".rclone_exclude"
_rclone_filters_path = repo_meta.get_local_part_path(config, RepoPart.CONF) / ".rclone_filters"

_rclone_include_path = _rclone_include_path if _rclone_include_path.exists() else None
_rclone_exclude_path = _rclone_exclude_path if _rclone_exclude_path.exists() else config.default_rclone_exclude_path
_rclone_filters_path = _rclone_filters_path if _rclone_filters_path.exists() else None

# %% [markdown]
# Sync the repo data

# %%
#|export
if check_interrupted(): raise SoftInterruption()

sync_part = RepoPart.DATA
if sync_part in sync_choices:
    if verbose: print("Syncing", sync_part.value)
    sync_results[sync_part] = await sync_helper(
        rclone_config_path=config.rclone_config_path,
        sync_direction=sync_direction,
        sync_setting=sync_setting,
        local_path=repo_meta.get_local_part_path(config, RepoPart.DATA),
        local_sync_record_path=repo_meta.get_local_sync_record_path(config, sync_part),
        remote=repo_meta.storage_location,
        remote_path=repo_meta.get_remote_part_path(config, RepoPart.DATA),
        remote_sync_record_path=repo_meta.get_remote_sync_record_path(config, sync_part),
        local_sync_backups_path=local_sync_backups_path,
        remote_sync_backups_path=remote_sync_backups_path,
        include_path=_rclone_include_path,
        exclude_path=_rclone_exclude_path,
        filters_path=_rclone_filters_path,
        verbose=verbose,
        show_rclone_progress=show_rclone_progress,
    )

# %%
# Check that the synced worked
from repoyard._utils import rclone_lsjson
_lsjson = await rclone_lsjson(
    rclone_config_path=config.rclone_config_path,
    source=repo_meta.storage_location,
    source_path=repo_meta.get_remote_part_path(config, RepoPart.DATA)
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

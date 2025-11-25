# %% [markdown]
# # _sync_missing_repometas

# %%
#|default_exp cmds._sync_missing_repometas
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path

from repoyard.config import get_config, StorageType
from repoyard._utils.sync_helper import SyncFailed, SyncUnsafe, InvalidRemotePath, SyncStatus, SyncSetting, SyncDirection
from repoyard._utils import check_interrupted, enable_soft_interruption, SoftInterruption
from repoyard import const


# %%
#|set_func_signature
async def sync_missing_repometas(
    config_path: Path,
    max_concurrent_rclone_ops: int|None = None,
    repo_full_names: list[str]|None = None,
    storage_locations: list[str]|None = None,
    sync_setting: SyncSetting = SyncSetting.CAREFUL,
    sync_direction: SyncDirection|None = None,
    verbose: bool = False,
    soft_interruption_enabled: bool = True,
) -> tuple[list[str], list[tuple[bool, SyncFailed|SyncUnsafe|InvalidRemotePath|None, SyncStatus, bool]]]:
    """
    """
    ...


# %% [markdown]
# Set up testing args

# %%
from tests.utils import *
remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %%
# Args (1/2)
config_path = config_path
max_concurrent_rclone_ops = None
repo_full_names = None
storage_locations = None
sync_direction = None
verbose = True
soft_interruption_enabled = True

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

if repo_full_names is not None and storage_locations is not None:
    raise ValueError("Cannot provide both `repo_full_names` and `storage_locations`.")

if max_concurrent_rclone_ops is None:
    max_concurrent_rclone_ops = config.max_concurrent_rclone_ops
    
if soft_interruption_enabled:
    enable_soft_interruption()

# %%
# Set up synced repos
from repoyard.cmds import new_repo, sync_repo
import asyncio
async def _task(i):
    repo_full_name = new_repo(config_path=config_path, repo_name=f"test_repo{i}", storage_location="my_remote")
    await sync_repo(config_path=config_path, repo_full_name=repo_full_name)
await asyncio.gather(*[_task(i) for i in range(3)]);

# %% [markdown]
# Sync remote repometas that have not been synced locally already (i.e. 'undiscovered' repometas)

# %%
#|export
if check_interrupted(): raise SoftInterruption()

from repoyard._utils import rclone_lsjson, rclone_sync, async_throttler
from repoyard._models import RepoMeta, SyncRecord, RepoPart, get_repoyard_meta

for sl_name, sl_config in config.storage_locations.items():
    if sl_config.storage_type == StorageType.LOCAL: continue

    if storage_locations is not None and sl_name not in storage_locations:
        continue
    
    # Get remote repometas
    _ls_remote = await rclone_lsjson(
        config.rclone_config_path,
        source=sl_name,
        source_path=sl_config.store_path / const.REMOTE_REPOS_REL_PATH,
        files_only=True,
        recursive=True,
        filter=[f"+ {const.REPO_METAFILE_REL_PATH}"],
        max_depth=2,
    )
    _ls_remote = {f["Path"] for f in _ls_remote} if _ls_remote else set()
    
    _ls_local = await rclone_lsjson(
        config.rclone_config_path,
        source="",
        source_path=config.local_store_path / sl_name,
        files_only=True,
        recursive=True,
        filter=[f"+ /{const.REPO_METAFILE_REL_PATH}"],
        max_depth=2,
    )
    _ls_local = {f["Path"] for f in _ls_local} if _ls_local else set()

    missing_metas = _ls_remote - _ls_local
    missing_repo_full_names = [Path(p).parts[0] for p in missing_metas]

    if repo_full_names is not None:
        missing_metas = [missing_meta for repo_full_name, missing_meta in zip(missing_repo_full_names, missing_metas) if repo_full_name in repo_full_names]

    if check_interrupted(): raise SoftInterruption()
    
    if len(missing_metas) > 0:
        if verbose:
            print(f"Syncing {len(missing_metas)} missing repometas from '{sl_name}'.")
            for missing_meta in missing_metas:
                print(f"  - {missing_meta}")

        await rclone_sync(
            rclone_config_path=config.rclone_config_path,
            source=sl_name,
            source_path=sl_config.store_path / const.REMOTE_REPOS_REL_PATH,
            dest="",
            dest_path=config.local_store_path / sl_name,
            filter=[f"+ /{p}" for p in missing_metas] + ["- **"],
            exclude=[],
        )

        # Create sync records
        async def _task(repo_full_name):
            repo_meta = RepoMeta.load(config, sl_name, repo_full_name) # Used to get the paths consistently
            rec = await SyncRecord.rclone_read(config.rclone_config_path, sl_name, repo_meta.get_remote_sync_record_path(config, RepoPart.META))
            await rec.rclone_save(config.rclone_config_path, "", repo_meta.get_local_sync_record_path(config, RepoPart.META))
        await async_throttler(
            [_task(repo_full_name) for repo_full_name in missing_repo_full_names],
            max_concurrency=max_concurrent_rclone_ops,
        )
    else:
        if verbose:
            print(f"No missing repometas in '{sl_name}' to sync.")

# %% [markdown]
# Refresh the repoyard meta file

# %%
#|export
from repoyard._models import refresh_repoyard_meta
refresh_repoyard_meta(config)

# %%
#|func_return
missing_metas;

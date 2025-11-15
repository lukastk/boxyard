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

from repoyard._utils.sync_helper import sync_helper, SyncSetting, SyncDirection
from repoyard.config import get_config, StorageType
from repoyard._utils.sync_helper import SyncFailed, SyncUnsafe, InvalidRemotePath, SyncStatus
from repoyard import const


# %%
#|set_func_signature
def sync_repometas(
    config_path: Path,
    repo_full_names: list[str]|None = None,
    storage_locations: list[str]|None = None,
    sync_setting: SyncSetting = SyncSetting.CAREFUL,
    sync_direction: SyncDirection|None = None,
    verbose: bool = False,
) -> tuple[list[str], list[tuple[bool, SyncFailed|SyncUnsafe|InvalidRemotePath|None, SyncStatus, bool]]]:
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
sync_direction = None
verbose = True

# %%
# Run init
from repoyard.cmds import init_repoyard
from repoyard.cmds import new_repo, sync_repo
init_repoyard(config_path=config_path, data_path=data_path)

# Add a storage location 'my_remote'
import toml
config_dump = toml.load(config_path)
remote_rclone_path = Path(tempfile.mkdtemp(prefix="rclone_remote", dir="/tmp"))
config_dump['storage_locations']['my_remote'] = {
    'storage_type' : "rclone",
    'store_path' : "repoyard",
}
config_path.write_text(toml.dumps(config_dump));

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

# Set up synced repos
for i in range(3):
    repo_full_name = new_repo(config_path=config_path, repo_name=f"test_repo{i}", storage_location="my_remote")
    sync_repo(config_path=config_path, repo_full_name=repo_full_name)

# %% [markdown]
# Sync remote repometas that have not been synced locally already (i.e. 'undiscovered' repometas)

# %%
#|export
from repoyard._utils import rclone_lsjson, rclone_sync
from repoyard._models import RepoMeta, SyncRecord, RepoPart, get_repoyard_meta

for sl_name, sl_config in config.storage_locations.items():
    if sl_config.storage_type == StorageType.LOCAL: continue

    if storage_locations is not None and sl_name not in storage_locations:
        continue
    
    # Get remote repometas
    _ls_remote = rclone_lsjson(
        config.rclone_config_path,
        source=sl_name,
        source_path=sl_config.store_path / const.REMOTE_REPOS_REL_PATH,
        files_only=True,
        recursive=True,
        filter=[f"+ {const.REPO_METAFILE_REL_PATH}"],
        max_depth=2,
    )
    _ls_remote = {f["Path"] for f in _ls_remote} if _ls_remote else set()

    _ls_local = rclone_lsjson(
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

    if len(missing_metas) > 0:
        rclone_sync(
            rclone_config_path=config.rclone_config_path,
            source=sl_name,
            source_path=sl_config.store_path / const.REMOTE_REPOS_REL_PATH,
            dest="",
            dest_path=config.local_store_path / sl_name,
            filter=[f"+ /{p}" for p in missing_metas] + ["- **"],
            exclude=[],
        )

        # Create sync records
        for repo_full_name in missing_repo_full_names:
            repo_meta = RepoMeta.load(config, sl_name, repo_full_name) # Used to get the paths consistently
            rec = SyncRecord.rclone_read(config.rclone_config_path, sl_name, repo_meta.get_remote_sync_record_path(config, RepoPart.META))
            rec.rclone_save(config.rclone_config_path, "", repo_meta.get_local_sync_record_path(config, RepoPart.META))

# %% [markdown]
# Sync the remaining repometas

# %%
# Modify a local repometa to test if it syncs properly
repoyard_meta = get_repoyard_meta(config)
repo_meta = list(repoyard_meta.by_full_name.values())[0]
repo_meta.groups = ["group1", "group2"]
repo_meta.save(config)

# %%
#|export
from repoyard.cmds._sync_repo import RepoPart, sync_repo
repoyard_meta = get_repoyard_meta(config)

repo_meta_sync_res = []

for repo_meta in repoyard_meta.by_full_name.values():
    if repo_full_names is not None and repo_meta.full_name not in repo_full_names:
        continue

    if storage_locations is not None and repo_meta.storage_location not in storage_locations:
        continue

    sync_res = None
    try:
        sync_res = sync_repo(
            config_path=config_path,
            repo_full_name=repo_meta.full_name,
            sync_direction=None,
            sync_setting=SyncSetting.CAREFUL,
            sync_choices=[RepoPart.META],
            verbose=verbose,
        )
        sync_pre_status, sync_happened = sync_res[RepoPart.META]
        repo_meta_sync_res.append((True, None, sync_pre_status, sync_happened))
    except SyncFailed as e:
        repo_meta_sync_res.append((False, e, sync_res, False))
    except SyncUnsafe as e:
        repo_meta_sync_res.append((False, e, sync_res, False))
    except InvalidRemotePath as e:
        repo_meta_sync_res.append((False, e, sync_res, False))

# %%
# Modify a local repometa to test if it syncs properly
from repoyard._models import get_repoyard_meta, RepoMeta
repoyard_meta = get_repoyard_meta(config)
repo_meta = list(repoyard_meta.by_full_name.values())[0]
import toml
_groups = toml.loads((remote_rclone_path / "repoyard" / const.REMOTE_REPOS_REL_PATH / repo_meta.full_name / "repometa.toml").read_text())["groups"]
assert "group1" in _groups
assert "group2" in _groups

# %% [markdown]
# Refresh the repoyard meta file

# %%
#|export
from repoyard._models import refresh_repoyard_meta
refresh_repoyard_meta(config)

# %%
#|func_return
missing_metas, repo_meta_sync_res;

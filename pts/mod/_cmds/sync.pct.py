# %% [markdown]
# # sync

# %%
#|default_exp _cmds.sync
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import repoyard._cmds.new as this_module

# %%
#|top_export
from pathlib import Path
import subprocess
import os

from repoyard._utils import get_synced_repo_full_name_from_sub_path
from repoyard.config import get_config
from repoyard import const


# %%
#|top_export
class _SyncError(Exception):
    def __init__(self, message: str, stdout: str, stderr: str):
        super().__init__(f"{message}\n\nrclone output:\n{stdout}\n{stderr}")
class Sync_RequiresForce(_SyncError): pass
class Sync_Conflict(_SyncError): pass
class Sync_Error(_SyncError): pass


# %%
#|set_func_signature
def sync(
    config_path: Path|None = None,
    repo_name: str|None = None,
    replace_remote: bool = False,
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
test_folder_path = Path(tempfile.mkdtemp(prefix="sync_test", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
if (tests_working_dir / "_cmds" / "sync").exists() or (tests_working_dir / "_cmds" / "sync").is_symlink():
    (tests_working_dir / "_cmds" / "sync").unlink()
(tests_working_dir / "_cmds" / "sync").symlink_to(test_folder_path, target_is_directory=True) # So that it can be viewed from within the project working directory
data_path = test_folder_path / ".repoyard"

# %%
# Args (1/2)
config_path = test_folder_path / "repoyard_config" / "config.toml"
replace_remote = False
force = False

# %%
# Run init
from repoyard._cmds.init import init
from repoyard._cmds.new import new
init(config_path=config_path, data_path=data_path)

# Add a storage location 'my_remote'
import toml
config_dump = toml.load(config_path)
remote_rclone_path = Path(tempfile.mkdtemp(prefix="rclone_remote", dir="/tmp"))
config_dump['storage_locations']['my_remote'] = {
    'storage_type' : "rclone",
    'repometa_path' : "repometa",
    'repostore_path' : "repostore",
}
config_path.write_text(toml.dumps(config_dump))

new(config_path=config_path, repo_name="test_repo", storage_location="my_remote")

# %%
# Args (2/2)
repo_name = list((data_path / "included_repostore").glob("*"))[0].name

# %%
# Put an excluded file into the repo to make sure it is not synced
(data_path / "included_repostore" / repo_name / ".venv").mkdir(parents=True, exist_ok=True)
(data_path / "included_repostore" / repo_name / ".venv" / "test.txt").write_text("test");

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
if config_path is None:
    config_path = const.DEFAULT_CONFIG_PATH
config = get_config(config_path)
    
if not repo_name:
    repo_name = get_synced_repo_full_name_from_sub_path(config, os.getcwd())
    if repo_name is None:
        raise ValueError("Working directory is not inside a Repoyard repo.")

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
from repoyard._repos import get_repoyard_meta
repoyard_meta = get_repoyard_meta(config)

if repo_name not in repoyard_meta.by_full_name:
    raise ValueError(f"Repo '{repo_name}' not found.")

repo_meta = repoyard_meta.by_full_name[repo_name]

# %% [markdown]
# Helper function for doing bisyncs

# %%
#|export
from repoyard._utils import rclone_bisync, rclone_sync, BisyncResult, rclone_mkdir, rclone_path_exists
_repoyard_ignore_path = repo_meta.get_included_repo_path(config) / ".repoyard_ignore"
_repoyard_filters_path = repo_meta.get_included_repo_path(config) / ".repoyard_filters"
_exclude_file = _repoyard_ignore_path if _repoyard_ignore_path.exists() else None
_filters_file = _repoyard_filters_path if _repoyard_filters_path.exists() else None

def bisync_helper(dry_run: bool, resync: bool, force: bool, return_command: bool=False) -> BisyncResult:
    # if not dry_run:
    #     rclone_mkdir(
    #         rclone_config_path=config.rclone_config_path,
    #         source=repo_meta.storage_location,
    #         source_path=config.storage_locations[repo_meta.storage_location].repostore_path,
    #     )
    #     rclone_mkdir(
    #         rclone_config_path=config.rclone_config_path,
    #         source=repo_meta.storage_location,
    #         source_path=config.storage_locations[repo_meta.storage_location].repometa_path,
    #     )
    return rclone_bisync(
        rclone_config_path=config.rclone_config_path,
        source="",
        source_path=repo_meta.get_included_repo_path(config),
        dest=repo_meta.storage_location,
        dest_path=repo_meta.get_remote_repo_path(config),
        exclude=[],
        exclude_file=_exclude_file,
        filters_file=_filters_file,
        dry_run=dry_run,
        resync=resync,
        force=force,
        return_command=return_command,
        verbose=False,
    )


# %% [markdown]
# Bisync (if `replace_remote` is False)

# %%
#|export
is_local = config.storage_locations[repo_meta.storage_location].storage_type == "local" # If the repo is local, we don't need to sync it.

# If the remote repo does not exist, we must sync instead of bisync
remote_repo_exists, remote_repo_is_dir = rclone_path_exists(
    rclone_config_path=config.rclone_config_path,
    source=repo_meta.storage_location,
    source_path=repo_meta.get_remote_repo_path(config),
)

if remote_repo_exists and not remote_repo_is_dir:
    raise Exception(f"Remote repo {repo_meta.full_name} is not a directory in remote {repo_meta.storage_location}.")

# %%
#|export
if not replace_remote and not is_local and remote_repo_exists:
    res, stdout, stderr = bisync_helper(
        dry_run=True,
        resync=False,
        force=False,
    )

    res_2 = None
    if BisyncResult.ERROR_NEEDS_RESYNC:
        res_2, stdout, stderr = bisync_helper(
            dry_run=False,
            resync=True,
            force=False,
        )
    elif BisyncResult.ERROR_ALL_FILES_CHANGED:
        if force:
            res, stdout, stderr = bisync_helper(
                dry_run=False,
                resync=True,
                force=True,
            )
        else:
            raise Sync_RequiresForce(f"All files in both local and remote repos have changed. Use `force=True` to force sync.", stdout, stderr)
    elif BisyncResult.CONFLICTS:
        raise Sync_Conflict(f"Conflicts found between local and remote repos.", stdout, stderr)
    elif BisyncResult.ERROR_OTHER:
        raise Sync_Error(f"Error.", stdout, stderr)
    elif BisyncResult.SUCCESS:
        res_2, stdout, stderr = bisync_helper(
            dry_run=False,
            resync=False,
            force=False,
        )
    else:
        raise ValueError(f"Unknown BisyncResult: {res}")

    if res_2 is not None:
        if res_2 != BisyncResult.SUCCESS:
            raise Sync_Error(f"Error.", stdout, stderr)

# %%
#|export
if (replace_remote or not remote_repo_exists) and not is_local:
    # Sync repo
    res, stdout, stderr = rclone_sync(
        rclone_config_path=config.rclone_config_path,
        source="",
        source_path=repo_meta.get_included_repo_path(config),
        dest=repo_meta.storage_location,
        dest_path=repo_meta.get_remote_repo_path(config),
        exclude=[],
        exclude_file=_exclude_file,
        filters_file=_filters_file,
        dry_run=False,
        verbose=False,
    )
    
    # Sync repometa
    res, stdout, stderr = rclone_sync(
        rclone_config_path=config.rclone_config_path,
        source="",
        source_path=repo_meta.get_synced_repometa_path(config),
        dest=repo_meta.storage_location,
        dest_path=config.storage_locations[repo_meta.storage_location].repometa_path,
        exclude=[],
        exclude_file=_exclude_file,
        filters_file=_filters_file,
        dry_run=False,
        verbose=False,
    )
    
    if not res:
        raise Sync_Error(f"Error.", stdout, stderr)

# %% [markdown]
# Check that the repo was synced successfully

# %%
from repoyard._utils import rclone_lsjson

_lsjson = rclone_lsjson(
    rclone_config_path=config.rclone_config_path,
    source=repo_meta.storage_location,
    source_path=repo_meta.get_remote_repo_path(config),
)

_names = {f["Name"] for f in _lsjson}
assert ".git" in _names
assert ".repoyard_ignore" in _names
assert ".venv" not in _names

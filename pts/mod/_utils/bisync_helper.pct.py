# %% [markdown]
# # bisync_helper

# %%
#|default_exp _utils.bisync_helper
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path
import subprocess
from typing import Literal
from enum import Enum

from repoyard import const


# %%
#|top_export
class SyncSetting(Enum):
    REPLACE_LOCAL = "replace_local"
    REPLACE_REMOTE = "replace_remote"
    BISYNC = "bisync"


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
def bisync_helper(
    rclone_config_path: str,
    sync_setting: SyncSetting,
    local_path: str,   
    remote: str,
    remote_path: str,
    force: bool,
    mkdir_if_missing: bool = True,
    include_path: Path|None = None,
    exclude_path: Path|None = None,
    filters_path: Path|None = None,
    include: list[str]|None = None,
    exclude: list[str]|None = None,
    filter: list[str]|None = None,
):
    """
    Helper to execute the standard routine for bisyncing a local and remote folder.
    """
    ...


# %% [markdown]
# Set up testing args

# %%
# Set up test environment
import tempfile
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = Path(tempfile.mkdtemp(prefix="bisync_helper", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
symlink_path = tests_working_dir / "_utils" / "bisync_helper"
symlink_path.parent.mkdir(parents=True, exist_ok=True)
if symlink_path.exists() or symlink_path.is_symlink():
    symlink_path.unlink()
symlink_path.symlink_to(test_folder_path, target_is_directory=True) # So that it can be viewed from within the project working directory
data_path = test_folder_path / ".repoyard"

# %%
my_local_path = test_folder_path / "my_local"
my_remote_path = test_folder_path / "my_remote"
my_local_path.mkdir(parents=True, exist_ok=True)
my_remote_path.mkdir(parents=True, exist_ok=True)

(my_local_path / "file1.txt").write_text("Hello, world!")
(my_local_path / "file2.txt").write_text("Goodbye, world!");
(my_local_path / "a_folder").mkdir(parents=True, exist_ok=True)
(my_local_path / "a_folder" / "file3.txt").write_text("Hello, world!");
(my_local_path / "a_folder" / "file4.txt").write_text("Goodbye, world!");

(my_remote_path / "file_on_remote.txt").write_text("Hello, world!") # Add a file on remote to ensure it doesn't get deleted during bisync --resync

# %%
(test_folder_path / "rclone.conf").write_text(f"""
[my_remote]
type = alias
remote = {test_folder_path / "my_remote"}
""")

# %%
# Args
rclone_config_path = test_folder_path / "rclone.conf"
sync_setting = SyncSetting.BISYNC
local_path = my_local_path
remote = "my_remote"
remote_path = ""
force = False
mkdir_if_missing = True
include_path = None
exclude_path = None
filters_path = None
include = None
exclude = None
filter = None

# %% [markdown]
# # Function body

# %%
#|export
from repoyard._utils import rclone_path_exists

# If the remote folder does not exist, we must sync instead of bisync
remote_exists, remote_repo_is_dir = rclone_path_exists(
    rclone_config_path=rclone_config_path,
    source=remote,
    source_path=remote_path,
)

if remote_exists and not remote_repo_is_dir:
    raise Exception(f"Remote folder '{remote_path}' is not a directory in remote {remote}.")

# %%
#|export
from repoyard._utils import rclone_bisync, rclone_sync, BisyncResult, rclone_mkdir, rclone_path_exists

def _bisync(dry_run: bool, resync: bool, force: bool, return_command: bool=False) -> BisyncResult:
    return rclone_bisync(
        rclone_config_path=rclone_config_path,
        source="",
        source_path=local_path,
        dest=remote,
        dest_path=remote_path,
        include=include or [],
        exclude=exclude or [],
        filter=filter or [],
        include_file=include_path,
        exclude_file=exclude_path,
        filters_file=filters_path,
        dry_run=dry_run,
        resync=resync,
        force=force,
        return_command=return_command,
        verbose=False,
    )
    
def _sync(dry_run: bool, source: str, source_path: str, dest: str, dest_path: str, return_command: bool=False,) -> BisyncResult:
    return rclone_sync(
        rclone_config_path=rclone_config_path,
        source=source,
        source_path=source_path,
        dest=dest,
        dest_path=dest_path,
        include=include or [],
        exclude=exclude or [],
        filter=filter or [],
        include_file=include_path,
        exclude_file=exclude_path,
        filters_file=filters_path,
        dry_run=dry_run,
        return_command=return_command,
        verbose=False,
    )


# %%
#|export
if mkdir_if_missing:
    rclone_mkdir(
        rclone_config_path=rclone_config_path,
        source="",
        source_path=local_path,
    )
    rclone_mkdir(
        rclone_config_path=rclone_config_path,
        source=remote,
        source_path=remote_path,
    )

# %%
#|export
if sync_setting == SyncSetting.BISYNC and remote_exists:
    # Dry run
    res_dry, stdout_dry, stderr_dry = _bisync(
        dry_run=True,
        resync=False,
        force=False,
    )

    res = None
    if BisyncResult.ERROR_NEEDS_RESYNC:
        res, stdout, stderr = _bisync(
            dry_run=False,
            resync=True,
            force=False,
        )
    elif BisyncResult.ERROR_ALL_FILES_CHANGED:
        if force:
            res_dry, stdout, stderr = _bisync(
                dry_run=False,
                resync=True,
                force=True,
            )
        else:
            raise Sync_RequiresForce(f"All files in both local and remote have changed. Use `force=True` to force sync.", stdout_dry, stderr_dry)
    elif BisyncResult.CONFLICTS:
        raise Sync_Conflict(f"Conflicts found between local and remote.", stdout_dry, stderr_dry)
    elif BisyncResult.ERROR_OTHER:
        raise Sync_Error(f"Error.", stdout_dry, stderr_dry)
    elif BisyncResult.SUCCESS:
        res, stdout, stderr = _bisync(
            dry_run=False,
            resync=False,
            force=False,
        )
    else:
        raise ValueError(f"Unknown BisyncResult: {res_dry}")

    if res is not None:
        if res != BisyncResult.SUCCESS:
            raise Sync_Error(f"Error.", stdout, stderr)
        
elif (sync_setting == SyncSetting.REPLACE_REMOTE or not remote_exists):
    res, stdout, stderr = _sync(
        dry_run=False,
        source="",
        source_path=local_path,
        dest=remote,
        dest_path=remote_path,
    )
    
    if not res:
        raise Sync_Error(f"Error.", stdout, stderr)
    
elif sync_setting == SyncSetting.REPLACE_LOCAL:
    res, stdout, stderr = _sync(
        dry_run=False,
        source=remote,
        source_path=remote_path,
        dest="",
        dest_path=local_path,
    )
    
    if not res:
        raise Sync_Error(f"Error.", stdout, stderr)
else:
    raise ValueError(f"Unknown sync setting: {sync_setting}")

# %% [markdown]
# Check that the sync worked

# %%
from repoyard._utils import rclone_lsjson

_lsjson = rclone_lsjson(
    rclone_config_path=rclone_config_path,
    source=remote,
    source_path=remote_path,
)

_names = {f["Name"] for f in _lsjson}
assert "a_folder" in _names
assert "file1.txt" in _names
assert "file2.txt" in _names
assert "file_on_remote.txt" in _names

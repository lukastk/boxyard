# %% [markdown]
# # _get_repo_sync_status

# %%
#|default_exp cmds._get_repo_sync_status
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path
from enum import Enum

from repoyard.config import get_config, StorageType
from repoyard._models import SyncStatus, RepoPart
from repoyard import const


# %%
#|set_func_signature
async def get_repo_sync_status(
    config_path: Path,
    repo_full_name: str,
) -> dict[RepoPart, SyncStatus]:
    """
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

# %%
#|export
from repoyard._models import get_sync_status, RepoPart
import asyncio

tasks = [get_sync_status(
    rclone_config_path=config.rclone_config_path,
    local_path=repo_meta.get_local_repometa_path(config),
    local_sync_record_path=repo_meta.get_local_sync_record_path(config, repo_part),
    remote=repo_meta.storage_location,
    remote_path=repo_meta.get_remote_repometa_path(config),
    remote_sync_record_path=repo_meta.get_remote_sync_record_path(config, repo_part),
) for repo_part in RepoPart]

repo_sync_status = {repo_part : sync_status for repo_part, sync_status in zip(RepoPart, await asyncio.gather(*tasks))}

# %%
from repoyard._models import SyncCondition
for repo_part in RepoPart:
    assert repo_sync_status[repo_part].sync_condition == SyncCondition.NEEDS_PUSH

# %%
#|func_return
repo_sync_status;

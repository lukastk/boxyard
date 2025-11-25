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
from tests.utils import *
remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %%
# Args
from repoyard.cmds import new_repo
config_path = config_path
repo_full_name = new_repo(config_path=config_path, repo_name="test_repo", storage_location=remote_name)

# %%
# Put an excluded file into the repo data folder to make sure it is not synced
(data_path / "local_store" / "my_remote" / repo_full_name / "test_repo" / ".venv").mkdir(parents=True, exist_ok=True)
(data_path / "local_store" / "my_remote" / repo_full_name / "test_repo" / ".venv" / "test.txt").write_text("test");

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

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
    local_path=repo_meta.get_local_part_path(config, RepoPart.META),
    local_sync_record_path=repo_meta.get_local_sync_record_path(config, repo_part),
    remote=repo_meta.storage_location,
    remote_path=repo_meta.get_remote_part_path(config, RepoPart.META),
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

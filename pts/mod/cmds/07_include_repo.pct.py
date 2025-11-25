# %% [markdown]
# # _include_repo

# %%
#|default_exp cmds._include_repo
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path

from repoyard.config import get_config
from repoyard import const


# %%
#|set_func_signature
async def include_repo(
    config_path: Path,
    repo_full_name: str,
    soft_interruption_enabled: bool = True,
):
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
repo_full_name = new_repo(config_path=config_path, repo_name="test_repo", storage_location="my_remote")
soft_interruption_enabled = True

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

# %%
from repoyard.cmds import sync_repo, exclude_repo
# Sync the repo
await sync_repo(config_path=config_path, repo_full_name=repo_full_name)
# Remove the repo from the local store to test the inclusion
await exclude_repo(config_path=config_path, repo_full_name=repo_full_name)

# %% [markdown]
# Check if repo is already included

# %%
#|export
from repoyard._models import get_repoyard_meta
repoyard_meta = get_repoyard_meta(config)

if repo_full_name not in repoyard_meta.by_full_name:
    raise ValueError(f"Repo '{repo_full_name}' does not exist.")

repo_meta = repoyard_meta.by_full_name[repo_full_name]

if repo_meta.check_included(config):
    raise ValueError(f"Repo '{repo_full_name}' is already included.")

# %% [markdown]
# Include it

# %%
#|export
from repoyard.cmds import sync_repo
from repoyard._models import RepoPart
from repoyard._utils.sync_helper import sync_helper, SyncSetting, SyncDirection

# First force sync the data
await sync_repo(
    config_path=config_path,
    repo_full_name=repo_full_name,
    sync_direction=SyncDirection.PULL,
    sync_setting=SyncSetting.FORCE,
    sync_choices=[RepoPart.DATA],
    soft_interruption_enabled=soft_interruption_enabled,
)

# Then sync the rest
await sync_repo(
    config_path=config_path,
    repo_full_name=repo_full_name,
    sync_direction=None,
    sync_setting=SyncSetting.CAREFUL,
    sync_choices=[RepoPart.META, RepoPart.CONF],
    soft_interruption_enabled=soft_interruption_enabled,
);

# %%
# Should now be included
assert repo_meta.check_included(config)

# %% [markdown]
# # _exclude_repo

# %%
#|default_exp cmds._exclude_repo
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path

from repoyard._utils.sync_helper import SyncSetting
from repoyard.config import get_config
from repoyard import const


# %%
#|set_func_signature
async def exclude_repo(
    config_path: Path,
    repo_full_name: str,
    skip_sync: bool = False,
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
# Args (1/2)
from repoyard.cmds import new_repo
config_path = config_path
repo_full_name = new_repo(config_path=config_path, repo_name="test_repo", storage_location="my_remote")
skip_sync = True
soft_interruption_enabled = True

# %%
from repoyard.cmds import sync_repo
await sync_repo(config_path=config_path, repo_full_name=repo_full_name, soft_interruption_enabled=soft_interruption_enabled);

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

# %% [markdown]
# Ensure that repo is included

# %%
#|export
from repoyard._models import get_repoyard_meta
repoyard_meta = get_repoyard_meta(config)

if repo_full_name not in repoyard_meta.by_full_name:
    raise ValueError(f"Repo '{repo_full_name}' does not exist.")

repo_meta = repoyard_meta.by_full_name[repo_full_name]

if not repo_meta.check_included(config):
    raise ValueError(f"Repo '{repo_full_name}' is already excluded.")

# %% [markdown]
# Check that the repo is not local

# %%
#|export
from repoyard.config import StorageType
if repo_meta.get_storage_location_config(config).storage_type == StorageType.LOCAL:
    raise ValueError(f"Repo '{repo_full_name}' in local storage location '{repo_meta.storage_location}' cannot be excluded.")

# %% [markdown]
# Sync any changes before removing locally

# %%
#|export
from repoyard.cmds import sync_repo

if not skip_sync:
    await sync_repo(
        config_path=config_path,
        repo_full_name=repo_full_name,
        sync_setting=SyncSetting.CAREFUL,
    )

# %% [markdown]
# Exclude it

# %%
#|export
import shutil
from repoyard._models import RepoPart
shutil.rmtree(repo_meta.get_local_part_path(config, RepoPart.DATA))
repo_meta.get_local_sync_record_path(config, RepoPart.DATA).unlink()

# %%
# Should now be included
assert not repo_meta.check_included(config)

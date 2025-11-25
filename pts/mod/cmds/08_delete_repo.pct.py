# %% [markdown]
# # _delete_repo

# %%
#|default_exp cmds._delete_repo
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path

from repoyard.config import get_config
from repoyard import const
from repoyard._utils import enable_soft_interruption


# %%
#|set_func_signature
async def delete_repo(
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

# %%
from repoyard.cmds import sync_repo
await sync_repo(config_path=config_path, repo_full_name=repo_full_name);

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

if soft_interruption_enabled:
    enable_soft_interruption()

# %% [markdown]
# Ensure that repo exists

# %%
#|export
from repoyard._models import get_repoyard_meta
repoyard_meta = get_repoyard_meta(config)

if repo_full_name not in repoyard_meta.by_full_name:
    raise ValueError(f"Repo '{repo_full_name}' does not exist.")

repo_meta = repoyard_meta.by_full_name[repo_full_name]

# %%
assert repo_meta.get_local_path(config).exists()
assert (remote_rclone_path / repo_meta.get_remote_path(config)).exists()

# %% [markdown]
# Delete the repo

# %%
#|export

# Delete local repo
import shutil
shutil.rmtree(repo_meta.get_local_path(config))

# Delete remote repo
from repoyard._utils import rclone_purge
from repoyard.config import StorageType
if repo_meta.get_storage_location_config(config).storage_type != StorageType.LOCAL:
    await rclone_purge(
        config.rclone_config_path,
        source=repo_meta.storage_location,
        source_path=repo_meta.get_remote_path(config),
    )

# %%
assert not repo_meta.get_local_path(config).exists()
assert not (remote_rclone_path / repo_meta.get_remote_path(config)).exists()

# %% [markdown]
# Refresh the repoyard meta file

# %%
#|export
from repoyard._models import refresh_repoyard_meta
refresh_repoyard_meta(config)

# %%
from repoyard._models import get_repoyard_meta
repoyard_meta = get_repoyard_meta(config)
assert len(repoyard_meta.by_full_name) == 0

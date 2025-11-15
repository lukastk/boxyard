# %% [markdown]
# # _modify_repometa

# %%
#|default_exp cmds._modify_repometa
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path
import subprocess
import os
from typing import Literal, Any

from repoyard._utils import get_repo_full_name_from_sub_path
from repoyard.config import get_config, StorageType
from repoyard import const


# %%
#|set_func_signature
def modify_repometa(
    config_path: Path,
    repo_full_name: str,
    modifications: dict[str, Any] = {},
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
test_folder_path = Path(tempfile.mkdtemp(prefix="modify_repometa", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
symlink_path = tests_working_dir / "_cmds" / "modify_repometa"
symlink_path.parent.mkdir(parents=True, exist_ok=True)
if symlink_path.exists() or symlink_path.is_symlink():
    symlink_path.unlink()
symlink_path.symlink_to(test_folder_path, target_is_directory=True) # So that it can be viewed from within the project working directory
data_path = test_folder_path / ".repoyard"

# %%
# Args (1/2)
config_path = test_folder_path / "repoyard_config" / "config.toml"
modifications = {
    'groups' : ['group1', 'group2']
}

# %%
# Run init
from repoyard.cmds import init_repoyard, new_repo, sync_repo
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

# Sync to remote
await sync_repo(config_path=config_path, repo_full_name=repo_full_name)

# %% [markdown]
# Find the repo meta

# %%
#|export
from repoyard._models import get_repoyard_meta, RepoMeta
repoyard_meta = get_repoyard_meta(config)

if repo_full_name not in repoyard_meta.by_full_name:
    raise ValueError(f"Repo '{repo_full_name}' not found.")

repo_meta = repoyard_meta.by_full_name[repo_full_name]

# %% [markdown]
# Modify repo meta

# %%
#|export
modified_repo_meta = RepoMeta(**{
    **repo_meta.model_dump(),
    **modifications
})

modified_repo_meta.save(config)

# %%
(config.local_store_path / "my_remote" / repo_full_name / "repometa.toml").read_text()

# %% [markdown]
# Refresh the repoyard meta file

# %%
#|export
from repoyard._models import refresh_repoyard_meta
refresh_repoyard_meta(config)

# %% [markdown]
# Check that the repometa has successfully updated on remote after syncing

# %%
import toml
from repoyard.cmds import sync_repometas
await sync_repometas(config_path=config_path)
repometa_dump = toml.load(remote_rclone_path / "repoyard" / const.REMOTE_REPOS_REL_PATH / repo_full_name / const.REPO_METAFILE_REL_PATH)
assert repometa_dump['groups'] == ['group1', 'group2']

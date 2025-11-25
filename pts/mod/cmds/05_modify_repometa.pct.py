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
#|top_export
class RepoNameConflict(Exception): pass


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
from tests.utils import *
remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %%
# Args (1/2)
from repoyard.cmds import new_repo
config_path = config_path
repo_full_name = new_repo(config_path=config_path, repo_name="test_repo", storage_location="my_remote")
modifications = {
    'groups' : ['group1', 'group2']
}

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

# %%
# Sync to remote
from repoyard.cmds import sync_repo
await sync_repo(config_path=config_path, repo_full_name=repo_full_name);

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
# Create modified repo meta

# %%
#|export
modified_repo_meta = RepoMeta(**{
    **repo_meta.model_dump(),
    **modifications
})

# %% [markdown]
# If the repo is in a group that requires unique names, check for conflicts

# %%
#|export
#TESTREF: test_modify_repometa_unique_names
from repoyard._models import get_repo_group_configs

# Construct modified repo_metas list
_old_repo_meta = repoyard_meta.by_full_name[repo_full_name]
_repo_metas = list(repoyard_meta.repo_metas)
_repo_metas.remove(_old_repo_meta)
_repo_metas.append(modified_repo_meta)

repo_group_configs, virtual_repo_groups = get_repo_group_configs(config, _repo_metas)
for g in modified_repo_meta.groups:
    if g in virtual_repo_groups:
        raise Exception(f"Cannot add a repository to a virtual repo group (virtual repo group: '{g}')")

    repo_group_config = repo_group_configs[g]
    repo_metas_in_group = [rm for rm in _repo_metas if g in modified_repo_meta.groups]

    if repo_group_config.unique_repo_names:
        name_counts = {repo_meta.name: 0 for repo_meta in repo_metas_in_group}
        for repo_meta in repo_metas_in_group:
            name_counts[repo_meta.name] += 1
        duplicate_names = [(name, count) for name, count in name_counts.items() if count > 1]
        if duplicate_names:
            names_str = ", ".join(f"'{name}' (count: {count})" for name, count in duplicate_names)
            raise RepoNameConflict(f"Error modifying repo meta for '{repo_full_name}':\n"
                             f"Repo is in group '{g}' which requires unique names. After the modification, the following name(s) appear multiple times in this group: {names_str}.")


# %% [markdown]
# Save the modified repo meta

# %%
#|export
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
from repoyard.cmds import sync_repo
from repoyard._models import RepoPart
await sync_repo(config_path=config_path, repo_full_name=repo_full_name, sync_choices=[RepoPart.META])
repometa_dump = toml.load(remote_rclone_path / "repoyard" / const.REMOTE_REPOS_REL_PATH / repo_full_name / const.REPO_METAFILE_REL_PATH)
assert repometa_dump['groups'] == ['group1', 'group2']

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _sync_name
#
# Synchronize the repo name between local and remote.
# This is useful when local and remote have different names for the same repo.

# %%
#|default_exp cmds._sync_name
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path

from repoyard.config import get_config, StorageType
from repoyard._remote_index import find_remote_repo_by_id
from repoyard._models import RepoMeta
from repoyard._enums import SyncNameDirection

# %%
#|set_func_signature
async def sync_name(
    config_path: Path,
    repo_index_name: str,
    direction: SyncNameDirection,
    verbose: bool = False,
) -> str:
    """
    Sync the repo name between local and remote.

    Args:
        config_path: Path to the repoyard config file.
        repo_index_name: Full index name of the local repository.
        direction: Direction to sync - to_local (remote name -> local) or
                  to_remote (local name -> remote).
        verbose: Print verbose output.

    Returns:
        The resulting index name after sync.

    Note:
        - TO_LOCAL: Renames the local repo to match the remote's name.
        - TO_REMOTE: Renames the remote repo to match the local's name.
    """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_repoyards

remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %%
# Args
from repoyard.cmds import new_repo, sync_repo

config_path = config_path
repo_index_name = new_repo(
    config_path=config_path, repo_name="test_repo", storage_location="my_remote"
)
await sync_repo(config_path=config_path, repo_index_name=repo_index_name)
direction = SyncNameDirection.TO_REMOTE
verbose = True

# %% [markdown]
# # Function body

# %% [markdown]
# Process args and get repo info

# %%
#|export
config = get_config(config_path)

from repoyard._models import get_repoyard_meta

repoyard_meta = get_repoyard_meta(config)

if repo_index_name not in repoyard_meta.by_index_name:
    raise ValueError(f"Repo '{repo_index_name}' not found.")

repo_meta = repoyard_meta.by_index_name[repo_index_name]
repo_id = RepoMeta.extract_repo_id(repo_index_name)
storage_location = repo_meta.storage_location
local_name = repo_meta.name

if verbose:
    print(f"Syncing name for repo ID: {repo_id}")
    print(f"Local name: {local_name}")

# %% [markdown]
# Check storage type

# %%
#|export
if repo_meta.get_storage_location_config(config).storage_type == StorageType.LOCAL:
    raise ValueError("Cannot sync name for local storage locations.")

# %% [markdown]
# Find remote repo and get its name

# %%
#|export
remote_index_name = await find_remote_repo_by_id(config, storage_location, repo_id)

if remote_index_name is None:
    raise ValueError(f"Remote repo not found for ID '{repo_id}'. Cannot sync name.")

_, remote_name = RepoMeta.parse_index_name(remote_index_name)

if verbose:
    print(f"Remote name: {remote_name}")

# %% [markdown]
# Determine source and target names

# %%
#|export
if direction == SyncNameDirection.TO_LOCAL:
    source_name = remote_name
    target_name = local_name
    action_desc = "remote -> local"
elif direction == SyncNameDirection.TO_REMOTE:
    source_name = local_name
    target_name = remote_name
    action_desc = "local -> remote"
else:
    raise ValueError(f"Invalid direction: {direction}")

if source_name == target_name:
    if verbose:
        print(f"Names already match: '{source_name}'. Nothing to do.")
    result_index_name = repo_index_name
    #|func_return_line

if verbose:
    print(f"Syncing name ({action_desc}): '{target_name}' -> '{source_name}'")

# %% [markdown]
# Perform the rename using the rename_repo command

# %%
#|export
from repoyard.cmds._rename_repo import rename_repo, RenameScope

if direction == SyncNameDirection.TO_LOCAL:
    # Rename local to match remote
    result_index_name = await rename_repo(
        config_path=config_path,
        repo_index_name=repo_index_name,
        new_name=source_name,
        scope=RenameScope.LOCAL,
        verbose=verbose,
    )
elif direction == SyncNameDirection.TO_REMOTE:
    # Rename remote to match local
    result_index_name = await rename_repo(
        config_path=config_path,
        repo_index_name=repo_index_name,
        new_name=source_name,
        scope=RenameScope.REMOTE,
        verbose=verbose,
    )

if verbose:
    print(f"Name sync complete. Result index: {result_index_name}")

# %%
#|func_return
result_index_name

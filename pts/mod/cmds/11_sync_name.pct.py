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
# Synchronize the box name between local and remote.
# This is useful when local and remote have different names for the same box.

# %%
#|default_exp cmds._sync_name
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path

from boxyard.config import get_config, StorageType
from boxyard._remote_index import find_remote_box_by_id
from boxyard._models import BoxMeta
from boxyard._enums import SyncNameDirection

# %%
#|set_func_signature
async def sync_name(
    config_path: Path,
    box_index_name: str,
    direction: SyncNameDirection,
    verbose: bool = False,
) -> str:
    """
    Sync the box name between local and remote.

    Args:
        config_path: Path to the boxyard config file.
        box_index_name: Full index name of the local box.
        direction: Direction to sync - to_local (remote name -> local) or
                  to_remote (local name -> remote).
        verbose: Print verbose output.

    Returns:
        The resulting index name after sync.

    Note:
        - TO_LOCAL: Renames the local box to match the remote's name.
        - TO_REMOTE: Renames the remote box to match the local's name.
    """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_boxyards

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %%
# Args
from boxyard.cmds import new_box, sync_box

config_path = config_path
box_index_name = new_box(
    config_path=config_path, box_name="test_box", storage_location="my_remote"
)
await sync_box(config_path=config_path, box_index_name=box_index_name)
direction = SyncNameDirection.TO_REMOTE
verbose = True

# %% [markdown]
# # Function body

# %% [markdown]
# Process args and get box info

# %%
#|export
config = get_config(config_path)

from boxyard._models import get_boxyard_meta

boxyard_meta = get_boxyard_meta(config)

if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' not found.")

box_meta = boxyard_meta.by_index_name[box_index_name]
box_id = BoxMeta.extract_box_id(box_index_name)
storage_location = box_meta.storage_location
local_name = box_meta.name

if verbose:
    print(f"Syncing name for box ID: {box_id}")
    print(f"Local name: {local_name}")

# %% [markdown]
# Check storage type

# %%
#|export
if box_meta.get_storage_location_config(config).storage_type == StorageType.LOCAL:
    raise ValueError("Cannot sync name for local storage locations.")

# %% [markdown]
# Find remote box and get its name

# %%
#|export
remote_index_name = await find_remote_box_by_id(config, storage_location, box_id)

if remote_index_name is None:
    raise ValueError(f"Remote box not found for ID '{box_id}'. Cannot sync name.")

_, remote_name = BoxMeta.parse_index_name(remote_index_name)

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
    result_index_name = box_index_name
    #|func_return_line

if verbose:
    print(f"Syncing name ({action_desc}): '{target_name}' -> '{source_name}'")

# %% [markdown]
# Perform the rename using the rename_box command

# %%
#|export
from boxyard.cmds._rename_box import rename_box, RenameScope

if direction == SyncNameDirection.TO_LOCAL:
    # Rename local to match remote
    result_index_name = await rename_box(
        config_path=config_path,
        box_index_name=box_index_name,
        new_name=source_name,
        scope=RenameScope.LOCAL,
        verbose=verbose,
    )
elif direction == SyncNameDirection.TO_REMOTE:
    # Rename remote to match local
    result_index_name = await rename_box(
        config_path=config_path,
        box_index_name=box_index_name,
        new_name=source_name,
        scope=RenameScope.REMOTE,
        verbose=verbose,
    )

if verbose:
    print(f"Name sync complete. Result index: {result_index_name}")

# %%
#|func_return
result_index_name

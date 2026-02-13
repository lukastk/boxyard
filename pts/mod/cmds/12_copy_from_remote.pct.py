# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _copy_from_remote

# %%
#|default_exp cmds._copy_from_remote
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path
import asyncio

from boxyard.config import get_config
from boxyard._models import get_boxyard_meta, BoxPart
from boxyard._remote_index import find_remote_box_by_id
from boxyard._utils.rclone import rclone_copy, rclone_copyto

# %%
#|set_func_signature
async def copy_from_remote(
    config_path: Path,
    box_index_name: str,
    dest_path: Path,
    copy_meta: bool = False,
    copy_conf: bool = False,
    overwrite: bool = False,
    show_rclone_progress: bool = False,
    verbose: bool = False,
) -> Path:
    """
    Copy a remote box's data to a local path without including it in boxyard.

    This command downloads the box data to any local path without:
    - Adding it to boxyard tracking
    - Creating sync records
    - Making it an "included" box

    Args:
        config_path: Path to the boxyard config file
        box_index_name: The index name of the box (local)
        dest_path: Destination path for the copy
        copy_meta: Also copy boxmeta.toml
        copy_conf: Also copy conf/ folder
        overwrite: Overwrite if dest exists
        show_rclone_progress: Show rclone progress output
        verbose: Print verbose output

    Returns:
        The destination path
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
import tempfile

config_path = config_path
box_index_name = new_box(
    config_path=config_path, box_name="test_box", storage_location="my_remote"
)
# Create a temp destination outside of boxyard paths
dest_path = Path(tempfile.mkdtemp()) / "copy_dest"
copy_meta = False
copy_conf = False
overwrite = False
show_rclone_progress = False
verbose = False

# %%
# Sync the box first so there's something on remote to copy
await sync_box(config_path=config_path, box_index_name=box_index_name)

# %% [markdown]
# # Function body

# %%
#|export
config = get_config(config_path)

# %%
#|export
boxyard_meta = get_boxyard_meta(config)

if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' does not exist locally.")

box_meta = boxyard_meta.by_index_name[box_index_name]

# %% [markdown]
# Safety check: Ensure dest_path is not within boxyard_data_path

# %%
#|export
dest_path = Path(dest_path).resolve()
boxyard_data_path = config.boxyard_data_path.resolve()

try:
    dest_path.relative_to(boxyard_data_path)
    raise ValueError(
        f"Destination path '{dest_path}' is within the boxyard data path '{boxyard_data_path}'. "
        f"This operation is not allowed to prevent conflicts with managed storage. "
        f"Use a path outside of '{boxyard_data_path}'."
    )
except ValueError as e:
    if "is within the boxyard data path" in str(e):
        raise
    # Good - dest_path is not within boxyard_data_path
    pass

# Also check user_boxes_path
user_boxes_path = config.user_boxes_path.resolve()
try:
    dest_path.relative_to(user_boxes_path)
    raise ValueError(
        f"Destination path '{dest_path}' is within the user boxes path '{user_boxes_path}'. "
        f"This operation is not allowed to prevent conflicts with managed boxes. "
        f"Use a path outside of '{user_boxes_path}'."
    )
except ValueError as e:
    if "is within the user boxes path" in str(e):
        raise
    # Good - dest_path is not within user_boxes_path
    pass

# %% [markdown]
# Check destination exists

# %%
#|export
if dest_path.exists() and not overwrite:
    raise ValueError(
        f"Destination path '{dest_path}' already exists. "
        f"Use --overwrite to overwrite existing files."
    )

# %% [markdown]
# Find the remote index name (may differ from local due to renames)

# %%
#|export
storage_location = box_meta.storage_location
sl_config = config.storage_locations[storage_location]

# Find remote index name by box_id (handles renames)
remote_index_name = await find_remote_box_by_id(
    config=config,
    storage_location=storage_location,
    box_id=box_meta.box_id,
)

if remote_index_name is None:
    raise ValueError(
        f"Box '{box_index_name}' not found on remote storage '{storage_location}'. "
        f"The box may have been deleted or the remote is not accessible."
    )

if verbose:
    print(f"Found remote box: {remote_index_name}")

# %% [markdown]
# Build remote paths

# %%
#|export
from boxyard import const

remote_box_path = sl_config.store_path / const.REMOTE_BOXES_REL_PATH / remote_index_name
remote_data_path = remote_box_path / const.BOX_DATA_REL_PATH
remote_meta_path = remote_box_path / const.BOX_METAFILE_REL_PATH
remote_conf_path = remote_box_path / const.BOX_CONF_REL_PATH

# %% [markdown]
# Copy DATA

# %%
#|export
if verbose:
    print(f"Copying DATA from {storage_location}:{remote_data_path} to {dest_path}")

# Create dest directory
dest_path.mkdir(parents=True, exist_ok=True)

success, stdout, stderr = await rclone_copy(
    rclone_config_path=config.rclone_config_path.as_posix(),
    source=storage_location,
    source_path=remote_data_path.as_posix(),
    dest="",
    dest_path=dest_path.as_posix(),
    progress=show_rclone_progress,
)

if not success:
    raise RuntimeError(f"Failed to copy DATA from remote: {stderr}")

if verbose:
    print("DATA copied successfully.")

# %% [markdown]
# Optionally copy META (boxmeta.toml)

# %%
#|export
if copy_meta:
    if verbose:
        print(f"Copying META from {storage_location}:{remote_meta_path}")

    dest_meta_path = dest_path / const.BOX_METAFILE_REL_PATH
    dest_meta_path.parent.mkdir(parents=True, exist_ok=True)

    success, stdout, stderr = await rclone_copyto(
        rclone_config_path=config.rclone_config_path.as_posix(),
        source=storage_location,
        source_path=remote_meta_path.as_posix(),
        dest="",
        dest_path=dest_meta_path.as_posix(),
        progress=show_rclone_progress,
    )

    if not success:
        if verbose:
            print(f"Warning: Failed to copy META: {stderr}")
    elif verbose:
        print("META copied successfully.")

# %% [markdown]
# Optionally copy CONF (conf/ folder)

# %%
#|export
if copy_conf:
    if verbose:
        print(f"Copying CONF from {storage_location}:{remote_conf_path}")

    dest_conf_path = dest_path / const.BOX_CONF_REL_PATH
    dest_conf_path.mkdir(parents=True, exist_ok=True)

    success, stdout, stderr = await rclone_copy(
        rclone_config_path=config.rclone_config_path.as_posix(),
        source=storage_location,
        source_path=remote_conf_path.as_posix(),
        dest="",
        dest_path=dest_conf_path.as_posix(),
        progress=show_rclone_progress,
    )

    if not success:
        if verbose:
            print(f"Warning: Failed to copy CONF: {stderr}")
    elif verbose:
        print("CONF copied successfully.")

# %% [markdown]
# Return destination path

# %%
#|func_return
dest_path;

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _tombstones
#
# Tombstone files are used to track deleted boxes. When a box is deleted,
# a tombstone file is created on the remote storage location. This allows other
# machines to discover that the box was deleted and handle it gracefully.

# %%
#|default_exp _tombstones

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
from pathlib import Path
from datetime import datetime, timezone

from boxyard import const
import boxyard.config

# %% [markdown]
# # `Tombstone` Model

# %%
#|export
class Tombstone(const.StrictModel):
    """
    A tombstone marks a deleted box.

    Stored at: {storage_location}:{store_path}/tombstones/{box_id}.json
    """
    box_id: str
    deleted_at_utc: datetime
    deleted_by_hostname: str
    last_known_name: str

# %% [markdown]
# # Tombstone Utilities

# %%
#|export
def get_tombstone_path(box_id: str) -> str:
    """Get the relative path for a tombstone file."""
    return f"tombstones/{box_id}.json"

# %%
#|export
async def create_tombstone(
    config: boxyard.config.Config,
    storage_location: str,
    box_id: str,
    last_known_name: str,
) -> Tombstone:
    """
    Create a tombstone for a deleted box on the remote storage.

    Args:
        config: Boxyard config
        storage_location: Name of the storage location
        box_id: The box ID being deleted
        last_known_name: The last known name of the box

    Returns:
        The created Tombstone
    """
    from boxyard._utils import get_hostname
    from boxyard._utils.rclone import rclone_write

    tombstone = Tombstone(
        box_id=box_id,
        deleted_at_utc=datetime.now(timezone.utc),
        deleted_by_hostname=get_hostname(),
        last_known_name=last_known_name,
    )

    sl_config = config.storage_locations[storage_location]
    tombstone_path = sl_config.store_path / get_tombstone_path(box_id)

    await rclone_write(
        rclone_config_path=config.rclone_config_path,
        dest=storage_location,
        dest_path=tombstone_path.as_posix(),
        content=tombstone.model_dump_json(indent=2),
    )

    return tombstone

# %%
#|export
async def is_tombstoned(
    config: boxyard.config.Config,
    storage_location: str,
    box_id: str,
) -> bool:
    """
    Check if a box_id has been tombstoned.

    Args:
        config: Boxyard config
        storage_location: Name of the storage location
        box_id: The box ID to check

    Returns:
        True if the box has been tombstoned, False otherwise
    """
    from boxyard._utils.rclone import rclone_path_exists

    sl_config = config.storage_locations[storage_location]
    tombstone_path = sl_config.store_path / get_tombstone_path(box_id)

    exists, _ = await rclone_path_exists(
        rclone_config_path=config.rclone_config_path,
        source=storage_location,
        source_path=tombstone_path.as_posix(),
    )
    return exists

# %%
#|export
async def get_tombstone(
    config: boxyard.config.Config,
    storage_location: str,
    box_id: str,
) -> Tombstone | None:
    """
    Get tombstone info for a box_id.

    Args:
        config: Boxyard config
        storage_location: Name of the storage location
        box_id: The box ID to look up

    Returns:
        Tombstone if found, None otherwise
    """
    from boxyard._utils.rclone import rclone_cat

    sl_config = config.storage_locations[storage_location]
    tombstone_path = sl_config.store_path / get_tombstone_path(box_id)

    exists, content = await rclone_cat(
        rclone_config_path=config.rclone_config_path,
        source=storage_location,
        source_path=tombstone_path.as_posix(),
    )

    if not exists or content is None:
        return None

    return Tombstone.model_validate_json(content)

# %%
#|export
async def list_tombstones(
    config: boxyard.config.Config,
    storage_location: str,
) -> list[Tombstone]:
    """
    List all tombstones for a storage location.

    Args:
        config: Boxyard config
        storage_location: Name of the storage location

    Returns:
        List of Tombstone objects
    """
    from boxyard._utils.rclone import rclone_lsjson, rclone_cat

    sl_config = config.storage_locations[storage_location]
    tombstones_dir = sl_config.store_path / "tombstones"

    files = await rclone_lsjson(
        rclone_config_path=config.rclone_config_path,
        source=storage_location,
        source_path=tombstones_dir.as_posix(),
    )

    if files is None:
        return []

    tombstones = []
    for f in files:
        if f.get("Name", "").endswith(".json") and not f.get("IsDir", False):
            file_path = tombstones_dir / f["Name"]
            exists, content = await rclone_cat(
                rclone_config_path=config.rclone_config_path,
                source=storage_location,
                source_path=file_path.as_posix(),
            )
            if exists and content:
                tombstones.append(Tombstone.model_validate_json(content))

    return tombstones

# %%
#|export
async def remove_tombstone(
    config: boxyard.config.Config,
    storage_location: str,
    box_id: str,
) -> None:
    """
    Remove a tombstone, allowing a box ID to be reused.

    Use for recovering from accidental deletions.

    Args:
        config: Boxyard config
        storage_location: Name of the storage location
        box_id: The box ID to untombstone

    Raises:
        ValueError: If no tombstone exists for the box ID
    """
    from boxyard._utils.rclone import rclone_delete, rclone_path_exists

    sl_config = config.storage_locations[storage_location]
    tombstone_path = sl_config.store_path / get_tombstone_path(box_id)

    exists, _ = await rclone_path_exists(
        rclone_config_path=config.rclone_config_path,
        source=storage_location,
        source_path=tombstone_path.as_posix(),
    )

    if not exists:
        raise ValueError(f"No tombstone found for box ID '{box_id}'")

    await rclone_delete(
        rclone_config_path=config.rclone_config_path,
        dest=storage_location,
        dest_path=tombstone_path.as_posix(),
    )

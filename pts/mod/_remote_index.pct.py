# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _remote_index
#
# Remote index cache utilities. This module provides a local cache that maps
# box IDs to their remote index names. This allows efficient lookups without
# having to scan the remote storage every time.

# %%
#|default_exp _remote_index

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
from pathlib import Path
import json

import boxyard.config
from boxyard import const

# %% [markdown]
# # Cache Utilities

# %%
#|export
def get_remote_index_cache_path(config: boxyard.config.Config, storage_location: str) -> Path:
    """Get the path to the remote index cache file for a storage location."""
    return config.remote_indexes_path / f"{storage_location}.json"

# %%
#|export
def load_remote_index_cache(config: boxyard.config.Config, storage_location: str) -> dict[str, str]:
    """
    Load the remote index cache for a storage location.

    Returns:
        Dict mapping box_id -> remote index_name
    """
    cache_path = get_remote_index_cache_path(config, storage_location)
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

# %%
#|export
def save_remote_index_cache(
    config: boxyard.config.Config,
    storage_location: str,
    cache: dict[str, str],
) -> None:
    """
    Save the remote index cache for a storage location.

    Args:
        config: Boxyard config
        storage_location: Name of the storage location
        cache: Dict mapping box_id -> remote index_name
    """
    cache_path = get_remote_index_cache_path(config, storage_location)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2))

# %%
#|export
def update_remote_index_cache(
    config: boxyard.config.Config,
    storage_location: str,
    box_id: str,
    index_name: str,
) -> None:
    """
    Update a single entry in the remote index cache.

    Args:
        config: Boxyard config
        storage_location: Name of the storage location
        box_id: The box ID
        index_name: The remote index_name for this box
    """
    cache = load_remote_index_cache(config, storage_location)
    cache[box_id] = index_name
    save_remote_index_cache(config, storage_location, cache)

# %%
#|export
def remove_from_remote_index_cache(
    config: boxyard.config.Config,
    storage_location: str,
    box_id: str,
) -> None:
    """
    Remove an entry from the remote index cache.

    Args:
        config: Boxyard config
        storage_location: Name of the storage location
        box_id: The box ID to remove
    """
    cache = load_remote_index_cache(config, storage_location)
    if box_id in cache:
        del cache[box_id]
        save_remote_index_cache(config, storage_location, cache)

# %% [markdown]
# # Finding Remote Repos by ID

# %%
#|export
async def find_remote_box_by_id(
    config: boxyard.config.Config,
    storage_location: str,
    box_id: str,
) -> str | None:
    """
    Find the remote index_name for a given box_id.

    Uses local cache first, falls back to remote scan if cache miss or stale.

    Args:
        config: Boxyard config
        storage_location: Name of the storage location
        box_id: The box ID to find

    Returns:
        The remote index_name if found, None otherwise
    """
    from boxyard._utils.rclone import rclone_path_exists, rclone_lsjson

    sl_config = config.storage_locations[storage_location]
    boxes_path = sl_config.store_path / const.REMOTE_BOXES_REL_PATH

    # 1. Check local cache
    cache = load_remote_index_cache(config, storage_location)
    if box_id in cache:
        cached_index_name = cache[box_id]
        # Verify it still exists on remote
        box_path = boxes_path / cached_index_name
        exists, _ = await rclone_path_exists(
            rclone_config_path=config.rclone_config_path,
            source=storage_location,
            source_path=box_path.as_posix(),
        )
        if exists:
            return cached_index_name
        # Cache is stale, remove entry
        del cache[box_id]
        save_remote_index_cache(config, storage_location, cache)

    # 2. Cache miss or stale - do full scan
    boxes = await rclone_lsjson(
        rclone_config_path=config.rclone_config_path,
        source=storage_location,
        source_path=boxes_path.as_posix(),
    )

    if boxes is not None:
        for item in boxes:
            if item.get("IsDir", False) and item.get("Name", "").startswith(f"{box_id}__"):
                found_index_name = item["Name"]
                # Update cache
                cache[box_id] = found_index_name
                save_remote_index_cache(config, storage_location, cache)
                return found_index_name

    # 3. Not found - ensure removed from cache
    if box_id in cache:
        del cache[box_id]
        save_remote_index_cache(config, storage_location, cache)

    return None

# %%
#|export
async def scan_and_rebuild_remote_index_cache(
    config: boxyard.config.Config,
    storage_location: str,
) -> dict[str, str]:
    """
    Scan remote storage and rebuild the entire cache for a storage location.

    Args:
        config: Boxyard config
        storage_location: Name of the storage location

    Returns:
        The rebuilt cache (box_id -> index_name)
    """
    from boxyard._utils.rclone import rclone_lsjson
    from boxyard._models import BoxMeta

    sl_config = config.storage_locations[storage_location]
    boxes_path = sl_config.store_path / const.REMOTE_BOXES_REL_PATH

    boxes = await rclone_lsjson(
        rclone_config_path=config.rclone_config_path,
        source=storage_location,
        source_path=boxes_path.as_posix(),
    )

    cache = {}
    if boxes is not None:
        for item in boxes:
            if item.get("IsDir", False):
                index_name = item["Name"]
                try:
                    box_id = BoxMeta.extract_box_id(index_name)
                    cache[box_id] = index_name
                except ValueError:
                    # Invalid index_name format, skip
                    pass

    save_remote_index_cache(config, storage_location, cache)
    return cache

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Copy From Remote Integration Tests
#
# Tests for the `copy` command that downloads a remote box to an arbitrary local path
# without including it in boxyard tracking.
#
# Tests:
# - Basic copy DATA to arbitrary path
# - Copy with --meta and --conf flags
# - Error without --overwrite when dest exists
# - Success with --overwrite
# - Error when dest is within boxyard data path (safety check)
# - Error when dest is within user boxes path (safety check)

# %%
#|default_exp integration.cmds.test_copy_from_remote
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import asyncio
import pytest
import tempfile
import shutil
from pathlib import Path

from boxyard.cmds import (
    new_box,
    sync_box,
)
from boxyard.cmds._copy_from_remote import copy_from_remote
from boxyard._models import get_boxyard_meta, BoxPart
from boxyard.config import get_config
from boxyard import const

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_copy_from_remote():
    """Test copy command for downloading remote boxes without tracking."""
    asyncio.run(_test_copy_from_remote())

# %%
#|set_func_signature
async def _test_copy_from_remote(): ...

# %% [markdown]
# ## Initialize boxyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %% [markdown]
# ## Create a box with some data

# %%
#|export
box_index_name = new_box(
    config_path=config_path,
    box_name="test-copy-box",
    storage_location=remote_name,
)

# Refresh config and get box meta
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta = boxyard_meta.by_index_name[box_index_name]

# Add some test data
local_data_path = box_meta.get_local_part_path(config, BoxPart.DATA)
test_file = local_data_path / "test_data.txt"
test_file.write_text("Hello from copy test!")

# Add nested directory
nested_dir = local_data_path / "nested" / "subdir"
nested_dir.mkdir(parents=True, exist_ok=True)
(nested_dir / "nested_file.txt").write_text("Nested content")

# Sync to remote
await sync_box(config_path=config_path, box_index_name=box_index_name)

# %% [markdown]
# ## Test 1: Basic copy DATA to arbitrary path

# %%
#|export
# Create temp directory for copy destination
with tempfile.TemporaryDirectory() as temp_dir:
    dest_path = Path(temp_dir) / "my_copy"

    result_path = await copy_from_remote(
        config_path=config_path,
        box_index_name=box_index_name,
        dest_path=dest_path,
        verbose=True,
    )

    assert result_path == dest_path.resolve()
    assert dest_path.exists()
    assert (dest_path / "test_data.txt").exists()
    assert (dest_path / "test_data.txt").read_text() == "Hello from copy test!"
    assert (dest_path / "nested" / "subdir" / "nested_file.txt").exists()
    assert (dest_path / "nested" / "subdir" / "nested_file.txt").read_text() == "Nested content"

    # Verify no boxmeta.toml by default
    assert not (dest_path / const.BOX_METAFILE_REL_PATH).exists()

    # Verify no conf folder by default
    assert not (dest_path / const.BOX_CONF_REL_PATH).exists()

# %% [markdown]
# ## Test 2: Copy with --meta flag

# %%
#|export
with tempfile.TemporaryDirectory() as temp_dir:
    dest_path = Path(temp_dir) / "my_copy_with_meta"

    result_path = await copy_from_remote(
        config_path=config_path,
        box_index_name=box_index_name,
        dest_path=dest_path,
        copy_meta=True,
        verbose=True,
    )

    assert dest_path.exists()
    assert (dest_path / "test_data.txt").exists()

    # Verify boxmeta.toml was copied
    assert (dest_path / const.BOX_METAFILE_REL_PATH).exists()

# %% [markdown]
# ## Test 3: Error without --overwrite when dest exists

# %%
#|export
with tempfile.TemporaryDirectory() as temp_dir:
    dest_path = Path(temp_dir) / "existing_dest"
    dest_path.mkdir(parents=True)
    (dest_path / "existing_file.txt").write_text("I exist already")

    try:
        await copy_from_remote(
            config_path=config_path,
            box_index_name=box_index_name,
            dest_path=dest_path,
        )
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "already exists" in str(e)
        assert "--overwrite" in str(e)

# %% [markdown]
# ## Test 4: Success with --overwrite

# %%
#|export
with tempfile.TemporaryDirectory() as temp_dir:
    dest_path = Path(temp_dir) / "existing_dest"
    dest_path.mkdir(parents=True)
    (dest_path / "existing_file.txt").write_text("I exist already")

    result_path = await copy_from_remote(
        config_path=config_path,
        box_index_name=box_index_name,
        dest_path=dest_path,
        overwrite=True,
        verbose=True,
    )

    assert dest_path.exists()
    assert (dest_path / "test_data.txt").exists()
    assert (dest_path / "test_data.txt").read_text() == "Hello from copy test!"

# %% [markdown]
# ## Test 5: Error when dest is within boxyard data path

# %%
#|export
# Try to copy to within boxyard data path
bad_dest = config.boxyard_data_path / "bad_copy_dest"

try:
    await copy_from_remote(
        config_path=config_path,
        box_index_name=box_index_name,
        dest_path=bad_dest,
    )
    assert False, "Should have raised ValueError"
except ValueError as e:
    assert "is within the boxyard data path" in str(e)

# %% [markdown]
# ## Test 6: Error when dest is within user boxes path

# %%
#|export
# Try to copy to within user boxes path
bad_dest = config.user_boxes_path / "bad_copy_dest"

try:
    await copy_from_remote(
        config_path=config_path,
        box_index_name=box_index_name,
        dest_path=bad_dest,
    )
    assert False, "Should have raised ValueError"
except ValueError as e:
    assert "is within the user boxes path" in str(e)

# %% [markdown]
# ## Test 7: Verify no sync records created

# %%
#|export
with tempfile.TemporaryDirectory() as temp_dir:
    dest_path = Path(temp_dir) / "no_sync_record_copy"

    await copy_from_remote(
        config_path=config_path,
        box_index_name=box_index_name,
        dest_path=dest_path,
    )

    # Verify no sync record was created in the destination
    # (This is implicit - there's nowhere for a sync record to be in an arbitrary path)
    assert dest_path.exists()
    assert not (dest_path / ".boxyard").exists()

# %% [markdown]
# ## Cleanup

# %%
#|export
from boxyard.cmds import delete_box

await delete_box(config_path=config_path, box_index_name=box_index_name)

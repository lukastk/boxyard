# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Force Push To Remote Integration Tests
#
# Tests for the `force-push` command that pushes a local folder to a box's remote DATA location.
#
# Tests:
# - Error without --force flag
# - Basic push from arbitrary path
# - Sync records updated correctly
# - Error when source doesn't exist
# - Push for excluded box (no local DATA)

# %%
#|default_exp integration.cmds.test_force_push_to_remote
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
    exclude_box,
    include_box,
)
from boxyard.cmds._force_push_to_remote import force_push_to_remote
from boxyard._models import get_boxyard_meta, BoxPart, SyncRecord
from boxyard.config import get_config
from boxyard import const
from boxyard._utils.rclone import rclone_lsjson

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_force_push_to_remote():
    """Test force-push command for pushing arbitrary folders to remote."""
    asyncio.run(_test_force_push_to_remote())

# %%
#|set_func_signature
async def _test_force_push_to_remote(): ...

# %% [markdown]
# ## Initialize boxyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %% [markdown]
# ## Create a box with some initial data

# %%
#|export
box_index_name = new_box(
    config_path=config_path,
    box_name="test-force-push-box",
    storage_location=remote_name,
)

# Refresh config and get box meta
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta = boxyard_meta.by_index_name[box_index_name]

# Add some initial test data
local_data_path = box_meta.get_local_part_path(config, BoxPart.DATA)
initial_file = local_data_path / "initial_file.txt"
initial_file.write_text("Initial content")

# Sync to remote
await sync_box(config_path=config_path, box_index_name=box_index_name)

# Verify initial file is on remote
remote_data_path = box_meta.get_remote_part_path(config, BoxPart.DATA)
remote_files = await rclone_lsjson(
    config.rclone_config_path,
    source=remote_name,
    source_path=str(remote_data_path),
)
assert any(f["Name"] == "initial_file.txt" for f in remote_files)

# %% [markdown]
# ## Test 1: Error without --force flag

# %%
#|export
with tempfile.TemporaryDirectory() as temp_dir:
    source_path = Path(temp_dir) / "my_source"
    source_path.mkdir(parents=True)
    (source_path / "new_file.txt").write_text("New content")

    try:
        await force_push_to_remote(
            config_path=config_path,
            box_index_name=box_index_name,
            source_path=source_path,
            force=False,  # Without --force
        )
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "destructive operation" in str(e)
        assert "--force" in str(e)

# %% [markdown]
# ## Test 2: Error when source doesn't exist

# %%
#|export
non_existent_path = Path("/tmp/non_existent_source_12345")
assert not non_existent_path.exists()

try:
    await force_push_to_remote(
        config_path=config_path,
        box_index_name=box_index_name,
        source_path=non_existent_path,
        force=True,
    )
    assert False, "Should have raised ValueError"
except ValueError as e:
    assert "does not exist" in str(e)

# %% [markdown]
# ## Test 3: Basic push from arbitrary path

# %%
#|export
with tempfile.TemporaryDirectory() as temp_dir:
    source_path = Path(temp_dir) / "my_source"
    source_path.mkdir(parents=True)
    (source_path / "new_file.txt").write_text("New content from force push")
    (source_path / "another_file.txt").write_text("Another new file")

    await force_push_to_remote(
        config_path=config_path,
        box_index_name=box_index_name,
        source_path=source_path,
        force=True,
        verbose=True,
    )

    # Verify new files are on remote
    remote_files = await rclone_lsjson(
        config.rclone_config_path,
        source=remote_name,
        source_path=str(remote_data_path),
    )
    file_names = [f["Name"] for f in remote_files]
    assert "new_file.txt" in file_names
    assert "another_file.txt" in file_names
    # Initial file should be gone (sync replaces everything)
    assert "initial_file.txt" not in file_names

# %% [markdown]
# ## Test 4: Sync records updated correctly

# %%
#|export
# Read sync records and verify they're complete
local_sync_record_path = box_meta.get_local_sync_record_path(config, BoxPart.DATA)
assert local_sync_record_path.exists()

local_record = SyncRecord.model_validate_json(local_sync_record_path.read_text())
assert local_record.sync_complete == True

# Read remote sync record
remote_sync_record = await SyncRecord.rclone_read(
    config.rclone_config_path,
    remote_name,
    str(box_meta.get_remote_sync_record_path(config, BoxPart.DATA)),
)
assert remote_sync_record is not None
assert remote_sync_record.sync_complete == True

# Verify ULIDs match
assert local_record.ulid == remote_sync_record.ulid

# %% [markdown]
# ## Test 5: Push for excluded box (no local DATA)

# %%
#|export
# First, exclude the box to remove local DATA
await exclude_box(config_path=config_path, box_index_name=box_index_name)

# Verify it's excluded
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta = boxyard_meta.by_index_name[box_index_name]
assert not box_meta.check_included(config)

# Now force push from an arbitrary source to the excluded box
with tempfile.TemporaryDirectory() as temp_dir:
    source_path = Path(temp_dir) / "restore_source"
    source_path.mkdir(parents=True)
    (source_path / "restored_file.txt").write_text("Restored content")

    await force_push_to_remote(
        config_path=config_path,
        box_index_name=box_index_name,
        source_path=source_path,
        force=True,
        verbose=True,
    )

    # Verify the file is on remote
    remote_files = await rclone_lsjson(
        config.rclone_config_path,
        source=remote_name,
        source_path=str(remote_data_path),
    )
    file_names = [f["Name"] for f in remote_files]
    assert "restored_file.txt" in file_names

# %% [markdown]
# ## Test 6: Include box and verify pushed content

# %%
#|export
# Include the box again
await include_box(config_path=config_path, box_index_name=box_index_name)

# Refresh config and verify the content
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta = boxyard_meta.by_index_name[box_index_name]

local_data_path = box_meta.get_local_part_path(config, BoxPart.DATA)
assert (local_data_path / "restored_file.txt").exists()
assert (local_data_path / "restored_file.txt").read_text() == "Restored content"

# %% [markdown]
# ## Cleanup

# %%
#|export
from boxyard.cmds import delete_box

await delete_box(config_path=config_path, box_index_name=box_index_name)

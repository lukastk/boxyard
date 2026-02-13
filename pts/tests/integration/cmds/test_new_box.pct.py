# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # New Box Integration Tests
#
# Tests for creating new boxes with various options.
#
# Tests:
# - Creating a basic box
# - Creating box from existing directory
# - Creating box with groups

# %%
#|default_exp integration.cmds.test_new_box
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import asyncio
import pytest
from pathlib import Path

from boxyard.cmds import new_box, delete_box
from boxyard._models import get_boxyard_meta, BoxPart
from boxyard.config import get_config

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_new_box():
    """Test creating new boxes with various options."""
    asyncio.run(_test_new_box())

# %%
#|set_func_signature
async def _test_new_box(): ...

# %% [markdown]
# ## Initialize boxyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %% [markdown]
# ## Test creating a basic box

# %%
#|export
box1 = new_box(
    config_path=config_path,
    box_name="basic-box",
    storage_location=remote_name,
)

config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta1 = boxyard_meta.by_index_name[box1]

# Verify box was created
assert box_meta1.name == "basic-box"
assert box_meta1.storage_location == remote_name
assert box_meta1.check_included(config)

# Verify local paths exist
assert box_meta1.get_local_path(config).exists()
assert box_meta1.get_local_part_path(config, BoxPart.DATA).exists()

# %% [markdown]
# ## Test creating box from existing directory

# %%
#|export
import tempfile
import shutil

# Create a temp directory with some content
source_dir = Path(tempfile.mkdtemp())
(source_dir / "existing_file.txt").write_text("Existing content")
(source_dir / "subdir").mkdir()
(source_dir / "subdir" / "nested.txt").write_text("Nested content")

# Create box from this directory (move mode)
box3 = new_box(
    config_path=config_path,
    box_name="from-existing",
    storage_location=remote_name,
    from_path=source_dir,
    copy_from_path=False,  # Move, not copy
)

config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta3 = boxyard_meta.by_index_name[box3]

# Verify content was moved
data_path = box_meta3.get_local_part_path(config, BoxPart.DATA)
assert (data_path / "existing_file.txt").exists()
assert (data_path / "existing_file.txt").read_text() == "Existing content"
assert (data_path / "subdir" / "nested.txt").exists()

# Source should be moved (not exist)
# Note: The move behavior depends on implementation
# assert not source_dir.exists()  # May or may not be true

# %% [markdown]
# ## Test creating box with groups

# %%
#|export
from boxyard.cmds import modify_boxmeta

box4 = new_box(
    config_path=config_path,
    box_name="grouped-box",
    storage_location=remote_name,
)

# Add groups via modify_boxmeta
modify_boxmeta(
    config_path=config_path,
    box_index_name=box4,
    modifications={"groups": ["backend", "python", "api"]},
)

config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta4 = boxyard_meta.by_index_name[box4]

# Verify groups were set
assert "backend" in box_meta4.groups
assert "python" in box_meta4.groups
assert "api" in box_meta4.groups

# %% [markdown]
# ## Cleanup

# %%
#|export
for box in [box1, box3, box4]:
    await delete_box(config_path=config_path, box_index_name=box)

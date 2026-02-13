# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Include/Exclude Integration Tests
#
# Tests for including and excluding boxes (sparse checkout functionality).
#
# Tests:
# - Excluding a box removes local data but keeps remote
# - Including a box downloads data from remote
# - Exclude pushes changes before removing local
# - Include/exclude preserves sync records

# %%
#|default_exp integration.cmds.test_include_exclude
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import asyncio
import pytest
from pathlib import Path

from boxyard.cmds import (
    new_box,
    sync_box,
    exclude_box,
    include_box,
)
from boxyard._models import get_boxyard_meta, BoxPart
from boxyard.config import get_config

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_include_exclude():
    """Test include/exclude operations for sparse checkout."""
    asyncio.run(_test_include_exclude())

# %%
#|set_func_signature
async def _test_include_exclude(): ...

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
    box_name="test-box",
    storage_location=remote_name,
)

# Refresh config and get box meta
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta = boxyard_meta.by_index_name[box_index_name]

# Add some test data
data_path = box_meta.get_local_part_path(config, BoxPart.DATA)
test_file = data_path / "test_data.txt"
test_file.write_text("Hello, World!")

# Sync to remote
await sync_box(config_path=config_path, box_index_name=box_index_name)

# %% [markdown]
# ## Verify box is included and data exists

# %%
#|export
assert box_meta.check_included(config)
assert test_file.exists()
assert test_file.read_text() == "Hello, World!"

# %% [markdown]
# ## Exclude the box

# %%
#|export
await exclude_box(config_path=config_path, box_index_name=box_index_name)

# Refresh config
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta = boxyard_meta.by_index_name[box_index_name]

# %% [markdown]
# ## Verify box is excluded - local data removed

# %%
#|export
assert not box_meta.check_included(config)
assert not test_file.exists()

# Verify remote still has data
from boxyard._utils import rclone_lsjson

remote_files = await rclone_lsjson(
    config.rclone_config_path,
    source=remote_name,
    source_path=str(box_meta.get_remote_path(config)) + "/data",
)
assert remote_files is not None
assert len(remote_files) > 0

# %% [markdown]
# ## Include the box again

# %%
#|export
await include_box(config_path=config_path, box_index_name=box_index_name)

# Refresh config
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta = boxyard_meta.by_index_name[box_index_name]

# %% [markdown]
# ## Verify box is included and data restored

# %%
#|export
assert box_meta.check_included(config)

# Data should be restored
data_path = box_meta.get_local_part_path(config, BoxPart.DATA)
test_file = data_path / "test_data.txt"
assert test_file.exists()
assert test_file.read_text() == "Hello, World!"

# %% [markdown]
# ## Test exclude pushes local changes first

# %%
#|export
# Make a local change
test_file.write_text("Modified content!")

# Exclude should push changes before removing
await exclude_box(config_path=config_path, box_index_name=box_index_name)

# Include again to verify changes were pushed
await include_box(config_path=config_path, box_index_name=box_index_name)

# Verify the modified content is there
config = get_config(config_path)
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta = boxyard_meta.by_index_name[box_index_name]
data_path = box_meta.get_local_part_path(config, BoxPart.DATA)
test_file = data_path / "test_data.txt"

assert test_file.read_text() == "Modified content!"

# %% [markdown]
# ## Cleanup

# %%
#|export
from boxyard.cmds import delete_box

await delete_box(config_path=config_path, box_index_name=box_index_name)

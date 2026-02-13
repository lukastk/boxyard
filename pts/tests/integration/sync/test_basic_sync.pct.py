# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Basic Sync Integration Tests
#
# Tests for basic box sync operations:
# - Creating boxes
# - Excluding boxes
# - Including boxes
# - Deleting boxes

# %%
#|default_exp integration.sync.test_basic_sync
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import asyncio
import pytest

from boxyard.cmds import (
    new_box,
    exclude_box,
    include_box,
    delete_box,
)
from boxyard._models import get_boxyard_meta

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_basic_sync():
    """Test basic sync operations: create, exclude, include, delete."""
    asyncio.run(_test_basic_sync())

# %%
#|set_func_signature
async def _test_basic_sync(): ...

# %% [markdown]
# ## Parameters

# %%
#|export
num_test_boxes = 5

# %% [markdown]
# ## Initialize boxyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %% [markdown]
# ## Create boxes using `new_box` and sync them

# %%
#|export
box_index_names = []
for i in range(num_test_boxes):
    box_index_name = new_box(
        config_path=config_path,
        box_name=f"test_box_{i}",
        storage_location=remote_name,
    )
    box_index_names.append(box_index_name)

# Verify that the boxes are included
boxyard_meta = get_boxyard_meta(config, force_create=True)
for box_index_name in box_index_names:
    assert boxyard_meta.by_index_name[box_index_name].check_included(config)

# %% [markdown]
# ## Exclude all boxes using `exclude_box`

# %%
#|export
await asyncio.gather(
    *[
        exclude_box(config_path=config_path, box_index_name=box_index_name)
        for box_index_name in box_index_names
    ]
)

# Verify that the boxes have been excluded
boxyard_meta = get_boxyard_meta(config, force_create=True)
for box_index_name in box_index_names:
    assert not boxyard_meta.by_index_name[box_index_name].check_included(config)

# %% [markdown]
# ## Include all boxes using `include_box`

# %%
#|export
await asyncio.gather(
    *[
        include_box(config_path=config_path, box_index_name=box_index_name)
        for box_index_name in box_index_names
    ]
)

# Verify that the boxes are included
boxyard_meta = get_boxyard_meta(config, force_create=True)
for box_index_name in box_index_names:
    assert boxyard_meta.by_index_name[box_index_name].check_included(config)

# %% [markdown]
# ## Delete all boxes using `delete_box`

# %%
#|export
await asyncio.gather(
    *[
        delete_box(config_path=config_path, box_index_name=box_index_name)
        for box_index_name in box_index_names
    ]
)

# Verify that the boxes have been deleted
for box_meta in boxyard_meta.by_index_name.values():
    assert not box_meta.get_local_path(config).exists()
    assert not (remote_rclone_path / box_meta.get_remote_path(config)).exists()

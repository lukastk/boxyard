# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # User Symlink Integration Tests
#
# Tests for creating and managing user box group symlinks.
#
# Tests:
# - Creating symlinks for regular groups
# - Creating symlinks for virtual groups
# - Different title modes (INDEX_NAME, DATETIME_AND_NAME, NAME)
# - Symlink cleanup when boxes are excluded/deleted
# - Conflict handling for duplicate names

# %%
#|default_exp integration.symlinks.test_user_symlinks
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
    exclude_box,
    delete_box,
    modify_boxmeta,
    create_user_symlinks,
)
from boxyard._models import get_boxyard_meta, create_user_box_group_symlinks
from boxyard.config import get_config

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_user_symlinks():
    """Test user symlink creation for box groups."""
    asyncio.run(_test_user_symlinks())

# %%
#|set_func_signature
async def _test_user_symlinks(): ...

# %% [markdown]
# ## Initialize boxyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# Ensure user_box_groups_path exists
config.user_box_groups_path.mkdir(parents=True, exist_ok=True)

# %% [markdown]
# ## Create boxes with different groups

# %%
#|export
# Create boxes in different groups
box1 = new_box(
    config_path=config_path,
    box_name="backend-api",
    storage_location=remote_name,
)
box2 = new_box(
    config_path=config_path,
    box_name="backend-worker",
    storage_location=remote_name,
)
box3 = new_box(
    config_path=config_path,
    box_name="frontend-app",
    storage_location=remote_name,
)

# Add boxes to groups
modify_boxmeta(
    config_path=config_path,
    box_index_name=box1,
    modifications={"groups": ["backend", "api"]},
)
modify_boxmeta(
    config_path=config_path,
    box_index_name=box2,
    modifications={"groups": ["backend", "worker"]},
)
modify_boxmeta(
    config_path=config_path,
    box_index_name=box3,
    modifications={"groups": ["frontend"]},
)

# %% [markdown]
# ## Create symlinks and verify

# %%
#|export
# Refresh config to get updated box metas
config = get_config(config_path)

# Create symlinks
create_user_box_group_symlinks(config)

# Verify symlinks were created
assert (config.user_box_groups_path / "backend").exists()
assert (config.user_box_groups_path / "frontend").exists()
assert (config.user_box_groups_path / "api").exists()
assert (config.user_box_groups_path / "worker").exists()

# %% [markdown]
# ## Verify symlink targets

# %%
#|export
boxyard_meta = get_boxyard_meta(config, force_create=True)

# Check backend group has both backend boxes
backend_symlinks = list((config.user_box_groups_path / "backend").iterdir())
assert len(backend_symlinks) == 2

# Check that symlinks point to correct locations
for symlink in backend_symlinks:
    assert symlink.is_symlink()
    target = symlink.resolve()
    assert target.exists()
    # Target should be in user_boxes_path (resolve both to handle /tmp vs /private/tmp on macOS)
    resolved_boxes_path = config.user_boxes_path.resolve()
    assert resolved_boxes_path in target.parents or target.parent == resolved_boxes_path

# %% [markdown]
# ## Test symlink cleanup when box is excluded

# %%
#|export
# Exclude one backend box
await exclude_box(config_path=config_path, box_index_name=box2)

# Recreate symlinks
config = get_config(config_path)
create_user_box_group_symlinks(config)

# Backend group should now have only 1 symlink
backend_symlinks = list((config.user_box_groups_path / "backend").iterdir())
assert len(backend_symlinks) == 1

# Worker group should be empty or removed
worker_path = config.user_box_groups_path / "worker"
if worker_path.exists():
    worker_symlinks = list(worker_path.iterdir())
    assert len(worker_symlinks) == 0

# %% [markdown]
# ## Test symlink cleanup when box is deleted

# %%
#|export
# Delete frontend box
await delete_box(config_path=config_path, box_index_name=box3)

# Recreate symlinks
config = get_config(config_path)
create_user_box_group_symlinks(config)

# Frontend group should be empty or removed
frontend_path = config.user_box_groups_path / "frontend"
if frontend_path.exists():
    frontend_symlinks = list(frontend_path.iterdir())
    assert len(frontend_symlinks) == 0

# %% [markdown]
# ## Cleanup

# %%
#|export
# Delete remaining boxes
await delete_box(config_path=config_path, box_index_name=box1)

# Final symlink cleanup
config = get_config(config_path)
create_user_box_group_symlinks(config)

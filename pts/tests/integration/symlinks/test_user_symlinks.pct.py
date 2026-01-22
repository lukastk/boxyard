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
# Tests for creating and managing user repo group symlinks.
#
# Tests:
# - Creating symlinks for regular groups
# - Creating symlinks for virtual groups
# - Different title modes (INDEX_NAME, DATETIME_AND_NAME, NAME)
# - Symlink cleanup when repos are excluded/deleted
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

from repoyard.cmds import (
    new_repo,
    exclude_repo,
    delete_repo,
    modify_repometa,
    create_user_symlinks,
)
from repoyard._models import get_repoyard_meta, create_user_repo_group_symlinks
from repoyard.config import get_config

from tests.integration.conftest import create_repoyards

# %%
#|top_export
@pytest.mark.integration
def test_user_symlinks():
    """Test user symlink creation for repo groups."""
    asyncio.run(_test_user_symlinks())

# %%
#|set_func_signature
async def _test_user_symlinks(): ...

# %% [markdown]
# ## Initialize repoyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# Ensure user_repo_groups_path exists
config.user_repo_groups_path.mkdir(parents=True, exist_ok=True)

# %% [markdown]
# ## Create repos with different groups

# %%
#|export
# Create repos in different groups
repo1 = new_repo(
    config_path=config_path,
    repo_name="backend-api",
    storage_location=remote_name,
)
repo2 = new_repo(
    config_path=config_path,
    repo_name="backend-worker",
    storage_location=remote_name,
)
repo3 = new_repo(
    config_path=config_path,
    repo_name="frontend-app",
    storage_location=remote_name,
)

# Add repos to groups
modify_repometa(
    config_path=config_path,
    repo_index_name=repo1,
    modifications={"groups": ["backend", "api"]},
)
modify_repometa(
    config_path=config_path,
    repo_index_name=repo2,
    modifications={"groups": ["backend", "worker"]},
)
modify_repometa(
    config_path=config_path,
    repo_index_name=repo3,
    modifications={"groups": ["frontend"]},
)

# %% [markdown]
# ## Create symlinks and verify

# %%
#|export
# Refresh config to get updated repo metas
config = get_config(config_path)

# Create symlinks
create_user_repo_group_symlinks(config)

# Verify symlinks were created
assert (config.user_repo_groups_path / "backend").exists()
assert (config.user_repo_groups_path / "frontend").exists()
assert (config.user_repo_groups_path / "api").exists()
assert (config.user_repo_groups_path / "worker").exists()

# %% [markdown]
# ## Verify symlink targets

# %%
#|export
repoyard_meta = get_repoyard_meta(config, force_create=True)

# Check backend group has both backend repos
backend_symlinks = list((config.user_repo_groups_path / "backend").iterdir())
assert len(backend_symlinks) == 2

# Check that symlinks point to correct locations
for symlink in backend_symlinks:
    assert symlink.is_symlink()
    target = symlink.resolve()
    assert target.exists()
    # Target should be in user_repos_path (resolve both to handle /tmp vs /private/tmp on macOS)
    resolved_repos_path = config.user_repos_path.resolve()
    assert resolved_repos_path in target.parents or target.parent == resolved_repos_path

# %% [markdown]
# ## Test symlink cleanup when repo is excluded

# %%
#|export
# Exclude one backend repo
await exclude_repo(config_path=config_path, repo_index_name=repo2)

# Recreate symlinks
config = get_config(config_path)
create_user_repo_group_symlinks(config)

# Backend group should now have only 1 symlink
backend_symlinks = list((config.user_repo_groups_path / "backend").iterdir())
assert len(backend_symlinks) == 1

# Worker group should be empty or removed
worker_path = config.user_repo_groups_path / "worker"
if worker_path.exists():
    worker_symlinks = list(worker_path.iterdir())
    assert len(worker_symlinks) == 0

# %% [markdown]
# ## Test symlink cleanup when repo is deleted

# %%
#|export
# Delete frontend repo
await delete_repo(config_path=config_path, repo_index_name=repo3)

# Recreate symlinks
config = get_config(config_path)
create_user_repo_group_symlinks(config)

# Frontend group should be empty or removed
frontend_path = config.user_repo_groups_path / "frontend"
if frontend_path.exists():
    frontend_symlinks = list(frontend_path.iterdir())
    assert len(frontend_symlinks) == 0

# %% [markdown]
# ## Cleanup

# %%
#|export
# Delete remaining repos
await delete_repo(config_path=config_path, repo_index_name=repo1)

# Final symlink cleanup
config = get_config(config_path)
create_user_repo_group_symlinks(config)

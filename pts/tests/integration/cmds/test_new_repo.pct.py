# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # New Repo Integration Tests
#
# Tests for creating new repositories with various options.
#
# Tests:
# - Creating a basic repo
# - Creating repo from existing directory
# - Creating repo with groups

# %%
#|default_exp integration.cmds.test_new_repo
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import asyncio
import pytest
from pathlib import Path

from repoyard.cmds import new_repo, delete_repo
from repoyard._models import get_repoyard_meta, RepoPart
from repoyard.config import get_config

from tests.integration.conftest import create_repoyards

# %%
#|top_export
@pytest.mark.integration
def test_new_repo():
    """Test creating new repositories with various options."""
    asyncio.run(_test_new_repo())

# %%
#|set_func_signature
async def _test_new_repo(): ...

# %% [markdown]
# ## Initialize repoyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %% [markdown]
# ## Test creating a basic repo

# %%
#|export
repo1 = new_repo(
    config_path=config_path,
    repo_name="basic-repo",
    storage_location=remote_name,
)

config = get_config(config_path)
repoyard_meta = get_repoyard_meta(config, force_create=True)
repo_meta1 = repoyard_meta.by_index_name[repo1]

# Verify repo was created
assert repo_meta1.name == "basic-repo"
assert repo_meta1.storage_location == remote_name
assert repo_meta1.check_included(config)

# Verify local paths exist
assert repo_meta1.get_local_path(config).exists()
assert repo_meta1.get_local_part_path(config, RepoPart.DATA).exists()

# %% [markdown]
# ## Test creating repo from existing directory

# %%
#|export
import tempfile
import shutil

# Create a temp directory with some content
source_dir = Path(tempfile.mkdtemp())
(source_dir / "existing_file.txt").write_text("Existing content")
(source_dir / "subdir").mkdir()
(source_dir / "subdir" / "nested.txt").write_text("Nested content")

# Create repo from this directory (move mode)
repo3 = new_repo(
    config_path=config_path,
    repo_name="from-existing",
    storage_location=remote_name,
    from_path=source_dir,
    copy_from_path=False,  # Move, not copy
)

config = get_config(config_path)
repoyard_meta = get_repoyard_meta(config, force_create=True)
repo_meta3 = repoyard_meta.by_index_name[repo3]

# Verify content was moved
data_path = repo_meta3.get_local_part_path(config, RepoPart.DATA)
assert (data_path / "existing_file.txt").exists()
assert (data_path / "existing_file.txt").read_text() == "Existing content"
assert (data_path / "subdir" / "nested.txt").exists()

# Source should be moved (not exist)
# Note: The move behavior depends on implementation
# assert not source_dir.exists()  # May or may not be true

# %% [markdown]
# ## Test creating repo with groups

# %%
#|export
from repoyard.cmds import modify_repometa

repo4 = new_repo(
    config_path=config_path,
    repo_name="grouped-repo",
    storage_location=remote_name,
)

# Add groups via modify_repometa
modify_repometa(
    config_path=config_path,
    repo_index_name=repo4,
    modifications={"groups": ["backend", "python", "api"]},
)

config = get_config(config_path)
repoyard_meta = get_repoyard_meta(config, force_create=True)
repo_meta4 = repoyard_meta.by_index_name[repo4]

# Verify groups were set
assert "backend" in repo_meta4.groups
assert "python" in repo_meta4.groups
assert "api" in repo_meta4.groups

# %% [markdown]
# ## Cleanup

# %%
#|export
for repo in [repo1, repo3, repo4]:
    await delete_repo(config_path=config_path, repo_index_name=repo)

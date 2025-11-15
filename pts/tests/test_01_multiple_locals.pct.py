# %% [markdown]
# # test_01_multiple_locals

# %%
#|default_exp test_01_multiple_locals
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import tests as this_module

# %%
#|top_export
import subprocess
from pathlib import Path
import shutil
import toml
import pytest

from repoyard import const
from repoyard.cmds import *
from repoyard._models import get_repoyard_meta, RepoPart
from repoyard._utils.sync_helper import SyncUnsafe

from tests.utils import *


# %%
#|set_func_signature
def test_01_multiple_locals(): ...


# %% [markdown]
# Parameters

# %%
#|export
num_repos = 5

# %% [markdown]
# Initialise two repoyards to simulate syncing conflicts

# %%
#|export
(
    sl_name, sl_rclone_path,
    [(config1, config_path1, data_path1), (config2, config_path2, data_path2)]
) = create_repoyards(num_repoyards=2)

# %% [markdown]
# Create some repos on repoyard 1 and sync them

# %%
#|export
repo_full_names = []
for i in range(num_repos):
    repo_full_name = new_repo(config_path=config_path1, repo_name=f"test_repo_{i}", storage_location=sl_name)
    sync_repo(config_path=config_path1, repo_full_name=repo_full_name)
    repo_full_names.append(repo_full_name)

# %% [markdown]
# Sync repometas into repoyard 2

# %%
#|export
sync_repometas(
    config_path=config_path2
)

repoyard_meta2 = get_repoyard_meta(config2)
assert len(repoyard_meta2.by_full_name) == num_repos

# %% [markdown]
# Ensure that the repometa sync only synced the repometas, and that sync records exists

# %%
for repo_meta in repoyard_meta2.repo_metas:
    assert repo_meta.get_local_sync_record_path(config2, RepoPart.META).exists()
    assert not repo_meta.check_included(config2)

# %% [markdown]
# Include them into repoyard 2

# %%
#|export
for repo_meta in repoyard_meta2.repo_metas:
    include_repo(
        config_path=config_path2,
        repo_full_name=repo_meta.full_name,
    )

# %% [markdown]
# Modify repos in repoyard 2 and sync

# %%
#|export
for repo_meta in repoyard_meta2.repo_metas:
    (repo_meta.get_local_repodata_path(config2) / "hello.txt").write_text("Hello, world!")

    sync_repo(
        config_path=config_path2,
        repo_full_name=repo_meta.full_name,
    )

# %% [markdown]
# Sync into repoyard 1

# %%
#|export
repoyard_meta1 = get_repoyard_meta(config1)
for repo_meta in repoyard_meta1.repo_metas:
    sync_repo(
        config_path=config_path1,
        repo_full_name=repo_meta.full_name,
    )

# %% [markdown]
# Verify that the sync worked

# %%
for repo_meta in repoyard_meta1.repo_metas:
    assert (repo_meta.get_local_repodata_path(config1) / "hello.txt").exists()

# %% [markdown]
# Create a conflict and test that it raises an exception

# %%
#|export
for repo_meta in repoyard_meta1.repo_metas:
    (repo_meta.get_local_repodata_path(config1) / "goodbye.txt").write_text("Goodbye, world!")
    sync_repo(
        config_path=config_path1,
        repo_full_name=repo_meta.full_name,
    )
    
    with pytest.raises(SyncUnsafe):
        (repo_meta.get_local_repodata_path(config2) / "goodbye.txt").write_text("I'm sorry, world!")
        sync_repo(
            config_path=config_path2,
            repo_full_name=repo_meta.full_name,
        )

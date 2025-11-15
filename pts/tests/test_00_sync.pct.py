# %% [markdown]
# # test_00_sync

# %%
#|default_exp test_00_sync
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
import asyncio

from repoyard import const
from repoyard.cmds import *
from repoyard._models import get_repoyard_meta

from tests.utils import *


# %%
#|top_export
def test_00_sync():
    asyncio.run(_test_00_sync())


# %%
#|set_func_signature
async def _test_00_sync(): ...


# %% [markdown]
# Parameters

# %%
#|export
num_test_repos = 5

# %% [markdown]
# # Initialise using `init_repoyard`

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %% [markdown]
# # Create some repos using `new_repo` and sync them using `sync_repo`

# %%
#|export
repo_full_names = []
for i in range(num_test_repos):
    repo_full_name = new_repo(config_path=config_path, repo_name=f"test_repo_{i}", storage_location=remote_name)
    repo_full_names.append(repo_full_name)
    
# Verify that the repos are included
repoyard_meta = get_repoyard_meta(config, force_create=True)
for repo_full_name in repo_full_names:
    assert repoyard_meta.by_full_name[repo_full_name].check_included(config)

# %% [markdown]
# # Exclude all repos using `exclude_repo`

# %%
#|export
await asyncio.gather(*[
    exclude_repo(config_path=config_path, repo_full_name=repo_full_name)
    for repo_full_name in repo_full_names
])
    
# Verify that the repos have been excluded
repoyard_meta = get_repoyard_meta(config, force_create=True)
for repo_full_name in repo_full_names:
    assert not repoyard_meta.by_full_name[repo_full_name].check_included(config)

# %% [markdown]
# # Include all repos using `include_repo`

# %%
#|export
await asyncio.gather(*[
    include_repo(config_path=config_path, repo_full_name=repo_full_name)
    for repo_full_name in repo_full_names
])
    
# Verify that the repos are included
repoyard_meta = get_repoyard_meta(config, force_create=True)
for repo_full_name in repo_full_names:
    assert repoyard_meta.by_full_name[repo_full_name].check_included(config)

# %% [markdown]
# # Delete all repos using `delete_repo`

# %%
#|export
await asyncio.gather(*[
    delete_repo(config_path=config_path, repo_full_name=repo_full_name)
    for repo_full_name in repo_full_names
])

# Verify that the repos have been deleted
for repo_meta in repoyard_meta.by_full_name.values():
    assert not repo_meta.get_local_path(config).exists()
    assert not (remote_rclone_path / repo_meta.get_remote_path(config)).exists()

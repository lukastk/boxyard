# %% [markdown]
# # test_02_remote

# %%
#|default_exp test_02_remote
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
import asyncio

from repoyard import const
from repoyard.cmds import *
from repoyard._models import get_repoyard_meta
from repoyard.config import get_config

from tests.utils import *

from dotenv import load_dotenv


# %%
#|top_export
def test_02_remote():
    asyncio.run(_test_02_remote())


# %%
#|set_func_signature
async def _test_02_remote(): ...


# %% [markdown]
# Load config from env var. If it doesn't exist then skip test.

# %%
#|export
from dotenv import load_dotenv
load_dotenv()
import os

if 'TEST_CONF_PATH' not in os.environ or 'TEST_STORAGE_LOCATION_NAME' not in os.environ or 'TEST_STORAGE_LOCATION_STORE_PATH' not in os.environ:
    pytest.skip("Environment variable TEST_CONF_PATH or TEST_STORAGE_LOCATION_NAME or TEST_STORAGE_LOCATION_STORE_PATH not set.")
else:
    config_path = Path(os.environ['TEST_CONF_PATH']).expanduser().resolve()
    config = get_config(config_path)
    sl_name = os.environ['TEST_STORAGE_LOCATION_NAME']
    sl_store_path = os.environ['TEST_STORAGE_LOCATION_STORE_PATH']

# %% [markdown]
# Ensure `repoyard` is installed

# %%
#|export
from tests.utils import run_cmd, run_cmd_in_background, CmdFailed

try:
    run_cmd("repoyard")
except CmdFailed:
    pytest.skip("repoyard not installed")

# %% [markdown]
# Create repo and sync it

# %%
#|export
repo_full_name1 = run_cmd(f"repoyard new -r test-repo-1 -g repoyard-unit-tests -s {sl_name}").strip()
run_cmd(f"repoyard sync -r {repo_full_name1}", capture_output=True);

# %% [markdown]
# Create two other repos and see they can both sync at the same time (i.e. test if simultaneous rclone commands can be run)

# %%
#|export
repo_full_name2 = run_cmd(f"repoyard new -r test-repo-2 -g repoyard-unit-tests -s {sl_name}").strip()
repo_full_name3 = run_cmd(f"repoyard new -r test-repo-3 -g repoyard-unit-tests -s {sl_name}").strip()

p1 = run_cmd_in_background(f"repoyard sync -r {repo_full_name2}", print_output=False)
p2 = run_cmd_in_background(f"repoyard sync -r {repo_full_name3}", print_output=False)

p1.wait()
p2.wait()

# %% [markdown]
# Verify that the repos are there on remote

# %%
#|export
from repoyard._models import get_repoyard_meta
repoyard_meta = get_repoyard_meta(config, force_create=True)
repo_meta1 = repoyard_meta.by_full_name[repo_full_name1]
repo_meta2 = repoyard_meta.by_full_name[repo_full_name2]
repo_meta3 = repoyard_meta.by_full_name[repo_full_name3]

from repoyard._utils import rclone_lsjson
for repo_meta in [repo_meta1, repo_meta2, repo_meta3]:
    assert rclone_lsjson(
        config.rclone_config_path,
        source=sl_name,
        source_path=repo_meta.get_remote_path(config),
    ) is not None

# %% [markdown]
# Exclude repos

# %%
#|export
for repo_meta in [repo_meta1, repo_meta2, repo_meta3]:
    run_cmd(f"repoyard exclude -r {repo_meta.full_name}")


# %%
#|export
async def _task(repo_meta):
    assert await rclone_lsjson(
        config.rclone_config_path,
        source="",
        source_path=repo_meta.get_local_repodata_path(config),
    ) is None

await asyncio.gather(*[_task(repo_meta) for repo_meta in [repo_meta1, repo_meta2, repo_meta3]]);

# %% [markdown]
# Re-include repos

# %%
#|export
for repo_meta in [repo_meta1, repo_meta2, repo_meta3]:
    run_cmd(f"repoyard include -r {repo_meta.full_name}")


# %%
#|export
async def _task(repo_meta):
    assert await rclone_lsjson(
        config.rclone_config_path,
        source="",
        source_path=repo_meta.get_local_repodata_path(config),
    ) is not None

await asyncio.gather(*[_task(repo_meta) for repo_meta in [repo_meta1, repo_meta2, repo_meta3]]);

# %% [markdown]
# Delete repos

# %%
#|export
for repo_meta in [repo_meta1, repo_meta2, repo_meta3]:
    run_cmd(f"repoyard delete -r {repo_meta.full_name}")


# %%
#|export
async def _task(repo_meta):
    assert await rclone_lsjson(
        config.rclone_config_path,
        source=sl_name,
        source_path=repo_meta.get_remote_path(config),
    ) is None

await asyncio.gather(*[_task(repo_meta) for repo_meta in [repo_meta1, repo_meta2, repo_meta3]]);

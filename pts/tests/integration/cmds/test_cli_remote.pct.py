# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # CLI Remote Integration Tests
#
# Tests for the repoyard CLI with a real remote storage backend.
#
# These tests require environment variables:
# - TEST_CONF_PATH: Path to the repoyard config file
# - TEST_STORAGE_LOCATION_NAME: Name of the storage location to use
# - TEST_STORAGE_LOCATION_STORE_PATH: Store path for the storage location
#
# Tests:
# - Creating and syncing repos via CLI
# - Concurrent sync operations
# - Exclude/include via CLI
# - Delete via CLI

# %%
#|default_exp integration.cmds.test_cli_remote
#|export_as_func true

# %%
#|hide
import nblite

nblite.nbl_export()

# %%
#|top_export
from pathlib import Path
import pytest
import asyncio

from repoyard.cmds import *
from repoyard._models import get_repoyard_meta, RepoPart
from repoyard.config import get_config

from tests.integration.conftest import run_cmd, run_cmd_in_background, CmdFailed

from dotenv import load_dotenv

# %%
#|top_export
@pytest.mark.integration
def test_cli_remote():
    """Test CLI commands with a real remote storage backend."""
    asyncio.run(_test_cli_remote())

# %%
#|set_func_signature
async def _test_cli_remote(): ...

# %% [markdown]
# ## Load config from environment variables

# %%
#|export
load_dotenv()
import os

if (
    "TEST_CONF_PATH" not in os.environ
    or "TEST_STORAGE_LOCATION_NAME" not in os.environ
    or "TEST_STORAGE_LOCATION_STORE_PATH" not in os.environ
):
    pytest.skip(
        "Environment variable TEST_CONF_PATH or TEST_STORAGE_LOCATION_NAME or "
        "TEST_STORAGE_LOCATION_STORE_PATH not set."
    )
else:
    config_path = Path(os.environ["TEST_CONF_PATH"]).expanduser().resolve()
    config = get_config(config_path)
    sl_name = os.environ["TEST_STORAGE_LOCATION_NAME"]
    sl_store_path = os.environ["TEST_STORAGE_LOCATION_STORE_PATH"]

# %% [markdown]
# ## Ensure repoyard CLI is installed

# %%
#|export
try:
    run_cmd("repoyard")
except CmdFailed:
    pytest.skip("repoyard not installed")

# %% [markdown]
# ## Create repo and sync it

# %%
#|export
repo_index_name1 = run_cmd(
    f"repoyard new -n test-repo-1 -g repoyard-unit-tests -s {sl_name}"
).strip()
run_cmd(f"repoyard sync -r {repo_index_name1}", capture_output=True)

# %% [markdown]
# ## Test concurrent sync operations

# %%
#|export
repo_index_name2 = run_cmd(
    f"repoyard new -n test-repo-2 -g repoyard-unit-tests -s {sl_name}"
).strip()
repo_index_name3 = run_cmd(
    f"repoyard new -n test-repo-3 -g repoyard-unit-tests -s {sl_name}"
).strip()

p1 = run_cmd_in_background(f"repoyard sync -r {repo_index_name2}", print_output=False)
p2 = run_cmd_in_background(f"repoyard sync -r {repo_index_name3}", print_output=False)

p1.wait()
p2.wait()

# %% [markdown]
# ## Verify repos exist on remote

# %%
#|export
repoyard_meta = get_repoyard_meta(config, force_create=True)
repo_meta1 = repoyard_meta.by_index_name[repo_index_name1]
repo_meta2 = repoyard_meta.by_index_name[repo_index_name2]
repo_meta3 = repoyard_meta.by_index_name[repo_index_name3]

from repoyard._utils import rclone_lsjson

for repo_meta in [repo_meta1, repo_meta2, repo_meta3]:
    assert (
        await rclone_lsjson(
            config.rclone_config_path,
            source=sl_name,
            source_path=repo_meta.get_remote_path(config),
        )
        is not None
    )

# %% [markdown]
# ## Exclude repos

# %%
#|export
for repo_meta in [repo_meta1, repo_meta2, repo_meta3]:
    run_cmd(f"repoyard exclude -r {repo_meta.index_name}")

# %%
#|export
async def _task(repo_meta):
    assert (
        await rclone_lsjson(
            config.rclone_config_path,
            source="",
            source_path=repo_meta.get_local_part_path(config, RepoPart.DATA),
        )
        is None
    )


await asyncio.gather(
    *[_task(repo_meta) for repo_meta in [repo_meta1, repo_meta2, repo_meta3]]
)

# %% [markdown]
# ## Re-include repos

# %%
#|export
for repo_meta in [repo_meta1, repo_meta2, repo_meta3]:
    run_cmd(f"repoyard include -r {repo_meta.index_name}")

# %%
#|export
async def _task(repo_meta):
    assert (
        await rclone_lsjson(
            config.rclone_config_path,
            source="",
            source_path=repo_meta.get_local_part_path(config, RepoPart.DATA),
        )
        is not None
    )


await asyncio.gather(
    *[_task(repo_meta) for repo_meta in [repo_meta1, repo_meta2, repo_meta3]]
)

# %% [markdown]
# ## Delete repos

# %%
#|export
for repo_meta in [repo_meta1, repo_meta2, repo_meta3]:
    run_cmd(f"repoyard delete -r {repo_meta.index_name}")

# %%
#|export
async def _task(repo_meta):
    assert (
        await rclone_lsjson(
            config.rclone_config_path,
            source=sl_name,
            source_path=repo_meta.get_remote_path(config),
        )
        is None
    )


await asyncio.gather(
    *[_task(repo_meta) for repo_meta in [repo_meta1, repo_meta2, repo_meta3]]
)

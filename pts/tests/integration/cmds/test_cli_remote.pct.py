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
# Tests for the boxyard CLI with a real remote storage backend.
#
# These tests require environment variables:
# - TEST_CONF_PATH: Path to the boxyard config file
# - TEST_STORAGE_LOCATION_NAME: Name of the storage location to use
# - TEST_STORAGE_LOCATION_STORE_PATH: Store path for the storage location
#
# Tests:
# - Creating and syncing boxes via CLI
# - Concurrent sync operations
# - Exclude/include via CLI
# - Delete via CLI

# %%
#|default_exp integration.cmds.test_cli_remote
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path
import pytest
import asyncio

from boxyard.cmds import *
from boxyard._models import get_boxyard_meta, BoxPart
from boxyard.config import get_config

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
# ## Ensure boxyard CLI is installed

# %%
#|export
try:
    run_cmd("boxyard")
except CmdFailed:
    pytest.skip("boxyard not installed")

# %% [markdown]
# ## Create box and sync it

# %%
#|export
box_index_name1 = run_cmd(
    f"boxyard new -n test-box-1 -g boxyard-unit-tests -s {sl_name}"
).strip()
run_cmd(f"boxyard sync -r {box_index_name1}", capture_output=True)

# %% [markdown]
# ## Test concurrent sync operations

# %%
#|export
box_index_name2 = run_cmd(
    f"boxyard new -n test-box-2 -g boxyard-unit-tests -s {sl_name}"
).strip()
box_index_name3 = run_cmd(
    f"boxyard new -n test-box-3 -g boxyard-unit-tests -s {sl_name}"
).strip()

p1 = run_cmd_in_background(f"boxyard sync -r {box_index_name2}", print_output=False)
p2 = run_cmd_in_background(f"boxyard sync -r {box_index_name3}", print_output=False)

p1.wait()
p2.wait()

# %% [markdown]
# ## Verify boxes exist on remote

# %%
#|export
boxyard_meta = get_boxyard_meta(config, force_create=True)
box_meta1 = boxyard_meta.by_index_name[box_index_name1]
box_meta2 = boxyard_meta.by_index_name[box_index_name2]
box_meta3 = boxyard_meta.by_index_name[box_index_name3]

from boxyard._utils import rclone_lsjson

for box_meta in [box_meta1, box_meta2, box_meta3]:
    assert (
        await rclone_lsjson(
            config.rclone_config_path,
            source=sl_name,
            source_path=box_meta.get_remote_path(config),
        )
        is not None
    )

# %% [markdown]
# ## Exclude boxes

# %%
#|export
for box_meta in [box_meta1, box_meta2, box_meta3]:
    run_cmd(f"boxyard exclude -r {box_meta.index_name}")

# %%
#|export
async def _task(box_meta):
    assert (
        await rclone_lsjson(
            config.rclone_config_path,
            source="",
            source_path=box_meta.get_local_part_path(config, BoxPart.DATA),
        )
        is None
    )


await asyncio.gather(
    *[_task(box_meta) for box_meta in [box_meta1, box_meta2, box_meta3]]
)

# %% [markdown]
# ## Re-include boxes

# %%
#|export
for box_meta in [box_meta1, box_meta2, box_meta3]:
    run_cmd(f"boxyard include -r {box_meta.index_name}")

# %%
#|export
async def _task(box_meta):
    assert (
        await rclone_lsjson(
            config.rclone_config_path,
            source="",
            source_path=box_meta.get_local_part_path(config, BoxPart.DATA),
        )
        is not None
    )


await asyncio.gather(
    *[_task(box_meta) for box_meta in [box_meta1, box_meta2, box_meta3]]
)

# %% [markdown]
# ## Delete boxes

# %%
#|export
for box_meta in [box_meta1, box_meta2, box_meta3]:
    run_cmd(f"boxyard delete -r {box_meta.index_name}")

# %%
#|export
async def _task(box_meta):
    assert (
        await rclone_lsjson(
            config.rclone_config_path,
            source=sl_name,
            source_path=box_meta.get_remote_path(config),
        )
        is None
    )


await asyncio.gather(
    *[_task(box_meta) for box_meta in [box_meta1, box_meta2, box_meta3]]
)

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _get_box_sync_status

# %%
#|default_exp cmds._get_box_sync_status
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path

from boxyard.config import get_config
from boxyard._models import SyncStatus, BoxPart

# %%
#|set_func_signature
async def get_box_sync_status(
    config_path: Path,
    box_index_name: str,
) -> dict[BoxPart, SyncStatus]:
    """ """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_boxyards

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %%
# Args
from boxyard.cmds import new_box

config_path = config_path
box_index_name = new_box(
    config_path=config_path, box_name="test_box", storage_location=remote_name
)

# %%
# Put an excluded file into the box data folder to make sure it is not synced
(
    data_path / "local_store" / "my_remote" / box_index_name / "test_box" / ".venv"
).mkdir(parents=True, exist_ok=True)
(
    data_path
    / "local_store"
    / "my_remote"
    / box_index_name
    / "test_box"
    / ".venv"
    / "test.txt"
).write_text("test")

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

# %% [markdown]
# Find the box meta

# %%
#|export
from boxyard._models import get_boxyard_meta

boxyard_meta = get_boxyard_meta(config)

if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' not found.")

box_meta = boxyard_meta.by_index_name[box_index_name]

# %%
#|export
from boxyard._models import get_sync_status, BoxPart
import asyncio

tasks = [
    get_sync_status(
        rclone_config_path=config.rclone_config_path,
        local_path=box_meta.get_local_part_path(config, box_part),
        local_sync_record_path=box_meta.get_local_sync_record_path(config, box_part),
        remote=box_meta.storage_location,
        remote_path=box_meta.get_remote_part_path(config, box_part),
        remote_sync_record_path=box_meta.get_remote_sync_record_path(
            config, box_part
        ),
    )
    for box_part in BoxPart
]

box_sync_status = {
    box_part: sync_status
    for box_part, sync_status in zip(BoxPart, await asyncio.gather(*tasks))
}

# %%
from boxyard._models import SyncCondition

for box_part in BoxPart:
    assert box_sync_status[box_part].sync_condition == SyncCondition.NEEDS_PUSH

# %%
#|func_return
box_sync_status

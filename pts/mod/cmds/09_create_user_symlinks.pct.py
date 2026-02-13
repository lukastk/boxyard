# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _create_user_symlinks

# %%
#|default_exp cmds._create_user_symlinks
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path

from boxyard.config import get_config

# %%
#|set_func_signature
def create_user_symlinks(
    config_path: Path,
    user_boxes_path: Path | None = None,
    user_box_groups_path: Path | None = None,
):
    """ """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_boxyards

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %%
# Args
config_path = config_path
user_boxes_path = None
user_box_groups_path = None

# %%

# %%
# Run init
from boxyard.cmds import new_box, modify_boxmeta

# Create a new box
box_index_name = new_box(config_path=config_path, box_name="test_box")
modify_boxmeta(
    config_path=config_path,
    box_index_name=box_index_name,
    modifications={
        "groups": ["test_group"],
    },
)

# Add a test group and demand unique box names in it to test the following
import toml

config_dump = toml.load(config_path)
config_dump["box_groups"] = {
    "test_group": {
        "box_title_mode": "name",
        "unique_box_names": True,
    }
}
config_path.write_text(toml.dumps(config_dump))

# Create a new box with the same name, to test the conflict handling when adding it to the same group
from boxyard.cmds._modify_boxmeta import BoxNameConflict

try:
    box_index_name2 = new_box(config_path=config_path, box_name="test_box")
    modify_boxmeta(
        config_path=config_path,
        box_index_name=box_index_name2,
        modifications={
            "groups": ["test_group"],
        },
    )
    raise ValueError("Should not happen")
except BoxNameConflict:
    pass

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

if user_boxes_path is None:
    user_boxes_path = config.user_boxes_path
if user_box_groups_path is None:
    user_box_groups_path = config.user_box_groups_path

# %% [markdown]
# Refresh the boxyard meta file

# %%
#|export
from boxyard._models import refresh_boxyard_meta

refresh_boxyard_meta(config)

# %%
ps = [p.name for p in config.user_boxes_path.glob("*")]
assert box_index_name in ps

# %% [markdown]
# Create box group symlinks

# %%
#|export
from boxyard._models import create_user_box_group_symlinks

create_user_box_group_symlinks(
    config=config,
)

# %%
assert (
    next(p.name for p in (config.user_box_groups_path / "test_group").glob("*"))
    == "test_box"
)

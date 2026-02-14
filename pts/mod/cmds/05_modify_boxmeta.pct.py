# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _modify_boxmeta

# %%
#|default_exp cmds._modify_boxmeta
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path
from typing import Any

from boxyard.config import get_config
from boxyard import const

# %%
#|top_export
class BoxNameConflict(Exception):
    pass

# %%
#|set_func_signature
def modify_boxmeta(
    config_path: Path,
    box_index_name: str,
    modifications: dict[str, Any] = {},
):
    """ """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_boxyards

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %%
# Args (1/2)
from boxyard.cmds import new_box

config_path = config_path
box_index_name = new_box(
    config_path=config_path, box_name="test_box", storage_location="my_remote"
)
modifications = {"groups": ["group1", "group2"]}

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

# %%
# Sync to remote
from boxyard.cmds import sync_box

await sync_box(config_path=config_path, box_index_name=box_index_name)

# %% [markdown]
# Find the box meta

# %%
#|export
from boxyard._models import get_boxyard_meta, BoxMeta

boxyard_meta = get_boxyard_meta(config)

if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' not found.")

box_meta = boxyard_meta.by_index_name[box_index_name]

# %% [markdown]
# Create modified box meta

# %%
#|export
modified_box_meta = BoxMeta(**{**box_meta.model_dump(), **modifications})

# %% [markdown]
# If the box is in a group that requires unique names, check for conflicts

# %%
#|export
# TESTREF: test_modify_boxmeta_unique_names
from boxyard._models import get_box_group_configs

# Construct modified box_metas list
_old_box_meta = boxyard_meta.by_index_name[box_index_name]
_box_metas = list(boxyard_meta.box_metas)
_box_metas.remove(_old_box_meta)
_box_metas.append(modified_box_meta)

box_group_configs, virtual_box_groups = get_box_group_configs(config, _box_metas)
for g in modified_box_meta.groups:
    if g in virtual_box_groups:
        raise Exception(
            f"Cannot add a box to a virtual box group (virtual box group: '{g}')"
        )

    box_group_config = box_group_configs[g]
    box_metas_in_group = [rm for rm in _box_metas if g in rm.groups]

    if box_group_config.unique_box_names:
        name_counts = {box_meta.name: 0 for box_meta in box_metas_in_group}
        for box_meta in box_metas_in_group:
            name_counts[box_meta.name] += 1
        duplicate_names = [
            (name, count) for name, count in name_counts.items() if count > 1
        ]
        if duplicate_names:
            names_str = ", ".join(
                f"'{name}' (count: {count})" for name, count in duplicate_names
            )
            raise BoxNameConflict(
                f"Error modifying box meta for '{box_index_name}':\n"
                f"Box is in group '{g}' which requires unique names. After the modification, the following name(s) appear multiple times in this group: {names_str}."
            )

# %% [markdown]
# Validate parents if they were modified

# %%
#|export
if "parents" in modifications:
    from boxyard._models import BoxyardMeta as _BM

    _temp_meta = _BM(box_metas=_box_metas)

    # Cycle detection
    for parent_id in modified_box_meta.parents:
        if _temp_meta.would_create_cycle(modified_box_meta.box_id, parent_id):
            raise ValueError(
                f"Adding parent '{parent_id}' to box '{box_index_name}' would create a cycle."
            )

    # Single-parent enforcement
    if config.single_parent and len(modified_box_meta.parents) > 1:
        raise ValueError(
            f"Config has single_parent=True but box '{box_index_name}' would have "
            f"{len(modified_box_meta.parents)} parents."
        )

    # Dangling parent warning
    import sys
    for parent_id in modified_box_meta.parents:
        if parent_id not in _temp_meta.by_id:
            print(
                f"Warning: parent '{parent_id}' not found locally. It may not be synced yet.",
                file=sys.stderr,
            )

# %% [markdown]
# Save the modified box meta

# %%
#|export
modified_box_meta.save(config)

# %%
(config.local_store_path / "my_remote" / box_index_name / "boxmeta.toml").read_text()

# %% [markdown]
# Refresh the boxyard meta file

# %%
#|export
from boxyard._models import refresh_boxyard_meta

refresh_boxyard_meta(config)

# %% [markdown]
# Check that the boxmeta has successfully updated on remote after syncing

# %%
import toml
from boxyard.cmds import sync_box
from boxyard._models import BoxPart

await sync_box(
    config_path=config_path,
    box_index_name=box_index_name,
    sync_choices=[BoxPart.META],
)
boxmeta_dump = toml.load(
    remote_rclone_path
    / "boxyard"
    / const.REMOTE_BOXES_REL_PATH
    / box_index_name
    / const.BOX_METAFILE_REL_PATH
)
assert boxmeta_dump["groups"] == ["group1", "group2"]

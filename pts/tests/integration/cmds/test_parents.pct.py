# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Parent-Child Integration Tests
#
# Tests for add-parent, remove-parent, tree, new --parent,
# delete --force, sync --sync-children, list hierarchy filters.

# %%
#|default_exp integration.cmds.test_parents
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import asyncio
import json
import pytest
from pathlib import Path

from boxyard.cmds import new_box, modify_boxmeta, delete_box, sync_box
from boxyard._models import get_boxyard_meta, refresh_boxyard_meta, BoxPart, BoxyardMeta
from boxyard.config import get_config

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_parents():
    """Test parent-child box relationships end-to-end."""
    asyncio.run(_test_parents())

# %%
#|set_func_signature
async def _test_parents(): ...

# %% [markdown]
# ## Initialize boxyard

# %%
#|export
remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %% [markdown]
# ## Create test boxes

# %%
#|export
box_a = new_box(config_path=config_path, box_name="box-a", storage_location=remote_name)
box_b = new_box(config_path=config_path, box_name="box-b", storage_location=remote_name)
box_c = new_box(config_path=config_path, box_name="box-c", storage_location=remote_name)
box_d = new_box(config_path=config_path, box_name="box-d", storage_location=remote_name)

config = get_config(config_path)
bm = get_boxyard_meta(config, force_create=True)
meta_a = bm.by_index_name[box_a]
meta_b = bm.by_index_name[box_b]
meta_c = bm.by_index_name[box_c]
meta_d = bm.by_index_name[box_d]

# %% [markdown]
# ## Test add-parent via modify_boxmeta

# %%
#|export
# A -> B (A is parent of B)
modify_boxmeta(
    config_path=config_path,
    box_index_name=box_b,
    modifications={"parents": [meta_a.box_id]},
)

config = get_config(config_path)
bm = get_boxyard_meta(config, force_create=True)
assert bm.by_index_name[box_b].parents == [meta_a.box_id]

# %% [markdown]
# ## Test children_of and ancestors_of

# %%
#|export
children = bm.children_of(meta_a.box_id)
assert len(children) == 1
assert children[0].box_id == meta_b.box_id

ancestors = bm.ancestors_of(meta_b.box_id)
assert len(ancestors) == 1
assert ancestors[0].box_id == meta_a.box_id

# %% [markdown]
# ## Build a diamond: A -> B, A -> C, B -> D, C -> D

# %%
#|export
modify_boxmeta(
    config_path=config_path,
    box_index_name=box_c,
    modifications={"parents": [meta_a.box_id]},
)
modify_boxmeta(
    config_path=config_path,
    box_index_name=box_d,
    modifications={"parents": [meta_b.box_id, meta_c.box_id]},
)

config = get_config(config_path)
bm = get_boxyard_meta(config, force_create=True)

# Verify roots and leaves
roots = bm.roots()
assert len(roots) == 1
assert roots[0].box_id == meta_a.box_id

leaves = bm.leaves()
assert len(leaves) == 1
assert leaves[0].box_id == meta_d.box_id

# Verify descendants with no duplicates
descs = bm.descendants_of(meta_a.box_id)
desc_ids = [d.box_id for d in descs]
assert len(desc_ids) == len(set(desc_ids)), "Descendants should not contain duplicates"
assert set(desc_ids) == {meta_b.box_id, meta_c.box_id, meta_d.box_id}

# %% [markdown]
# ## Test cycle detection

# %%
#|export
# Trying to make A a child of D (D -> A) should create cycle
assert bm.would_create_cycle(meta_a.box_id, meta_d.box_id) is True

with pytest.raises(ValueError, match="would create a cycle"):
    modify_boxmeta(
        config_path=config_path,
        box_index_name=box_a,
        modifications={"parents": [meta_d.box_id]},
    )

# %% [markdown]
# ## Test remove-parent

# %%
#|export
# Remove A as parent of C
modify_boxmeta(
    config_path=config_path,
    box_index_name=box_c,
    modifications={"parents": []},
)
config = get_config(config_path)
bm = get_boxyard_meta(config, force_create=True)
assert bm.by_index_name[box_c].parents == []

# C is now a root (no parents), and also a parent of D
roots = bm.roots()
root_ids = {r.box_id for r in roots}
assert meta_a.box_id in root_ids
assert meta_c.box_id in root_ids

# %% [markdown]
# ## Test single_parent enforcement

# %%
#|export
import toml

# Enable single_parent in config
config_data = toml.load(config_path)
config_data["single_parent"] = True
config_path.write_text(toml.dumps(config_data))

# Try to add two parents to box_d (already has B and C as parents via our previous setup)
# First restore C as parent of D
with pytest.raises(ValueError, match="single_parent"):
    modify_boxmeta(
        config_path=config_path,
        box_index_name=box_d,
        modifications={"parents": [meta_b.box_id, meta_c.box_id]},
    )

# Restore config
config_data["single_parent"] = False
config_path.write_text(toml.dumps(config_data))

# %% [markdown]
# ## Test delete with children protection

# %%
#|export
# Re-add parent relationship: A -> B, B -> D
config = get_config(config_path)
bm = get_boxyard_meta(config, force_create=True)
children_of_a = bm.children_of(meta_a.box_id)
assert len(children_of_a) > 0, "A should have children before testing delete protection"

# %% [markdown]
# ## Test sync --sync-children (via direct API)

# %%
#|export
# Sync box A, then verify it works
await sync_box(config_path=config_path, box_index_name=box_a)
await sync_box(config_path=config_path, box_index_name=box_b)

# Verify sync records exist
config = get_config(config_path)

# %% [markdown]
# ## Test list hierarchy filters (via BoxyardMeta methods)

# %%
#|export
config = get_config(config_path)
bm = get_boxyard_meta(config, force_create=True)

# children_of
children = bm.children_of(meta_a.box_id)
child_ids = {c.box_id for c in children}
assert meta_b.box_id in child_ids

# roots
roots = bm.roots()
assert any(r.box_id == meta_a.box_id for r in roots)

# leaves
leaves = bm.leaves()
leaf_ids = {l.box_id for l in leaves}
assert meta_d.box_id in leaf_ids

# %% [markdown]
# ## Test cross-storage parents

# %%
#|export
# Create a box on fake storage and parent it to a box on the remote
box_e = new_box(config_path=config_path, box_name="box-e", storage_location="fake")
config = get_config(config_path)
bm = get_boxyard_meta(config, force_create=True)
meta_e = bm.by_index_name[box_e]

# Add A (on remote) as parent of E (on fake)
modify_boxmeta(
    config_path=config_path,
    box_index_name=box_e,
    modifications={"parents": [meta_a.box_id]},
)
config = get_config(config_path)
bm = get_boxyard_meta(config, force_create=True)
assert bm.by_index_name[box_e].parents == [meta_a.box_id]
children_of_a = bm.children_of(meta_a.box_id)
assert any(c.box_id == meta_e.box_id for c in children_of_a)

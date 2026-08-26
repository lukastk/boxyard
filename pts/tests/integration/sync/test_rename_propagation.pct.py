# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Rename Propagation Integration Tests
#
# A box's index name is `{box_id}__{name}`, and a rename changes only the name
# half — the id is preserved. `sync_missing_boxmetas` used to diff on the raw
# index-name path, so a renamed box looked like a brand-new one; and because
# that pass only ever ADDS, every other machine ended up holding TWO
# registrations for one box id: the stale pre-rename name, which nothing removed,
# plus the newly-fetched one.
#
# That is what `doctor`'s `duplicate-box-id` was reporting on macbook, macstudio
# and ideapad in Aug 2026 — three boxes each, identical on all three machines,
# and absent on the machine that had done the renames.

# %%
#|default_exp integration.sync.test_rename_propagation
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import pytest
import asyncio

from boxyard.cmds import new_box, sync_box, sync_missing_boxmetas
from boxyard.cmds._rename_box import rename_box
from boxyard._enums import RenameScope
from boxyard._models import get_boxyard_meta, BoxMeta

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_rename_propagates_without_duplicating():
    """A rename on one machine is adopted by another, not duplicated."""
    asyncio.run(_test_rename_propagates_without_duplicating())

# %%
#|set_func_signature
async def _test_rename_propagates_without_duplicating(): ...

# %% [markdown]
# ## Two boxyards sharing one remote, simulating two machines

# %%
#|export
(
    sl_name,
    sl_rclone_path,
    [(config1, config_path1, data_path1), (config2, config_path2, data_path2)],
) = create_boxyards(num_boxyards=2)

# %% [markdown]
# ## Machine 1 creates a box and pushes it

# %%
#|export
original_index_name = new_box(
    config_path=config_path1, box_name="before-rename", storage_location=sl_name
)
box_id = BoxMeta.extract_box_id(original_index_name)
await sync_box(config_path=config_path1, box_index_name=original_index_name)

# %% [markdown]
# ## Machine 2 learns about it

# %%
#|export
await sync_missing_boxmetas(config_path=config_path2)

meta2 = get_boxyard_meta(config2, force_create=True)
registrations = [bm for bm in meta2.box_metas if bm.box_id == box_id]
assert len(registrations) == 1, f"expected one registration, got {[b.index_name for b in registrations]}"
assert registrations[0].name == "before-rename"

# %% [markdown]
# ## Machine 1 renames the box on both sides

# %%
#|export
new_index_name = await rename_box(
    config_path=config_path1,
    box_index_name=original_index_name,
    new_name="after-rename",
    scope=RenameScope.BOTH,
)
assert new_index_name == f"{box_id}__after-rename"

# %% [markdown]
# ## Machine 2 syncs again — and must ADOPT the new name, not duplicate it

# %%
#|export
await sync_missing_boxmetas(config_path=config_path2)

meta2 = get_boxyard_meta(config2, force_create=True)
registrations = [bm for bm in meta2.box_metas if bm.box_id == box_id]

assert len(registrations) == 1, (
    "the rename produced a DUPLICATE registration instead of being adopted: "
    f"{[b.index_name for b in registrations]}"
)
assert registrations[0].name == "after-rename", (
    f"machine 2 kept the stale name {registrations[0].name!r}"
)

# %% [markdown]
# ## The old registration is gone from disk, not merely absent from the cache

# %%
#|export
old_local_store_path = config2.local_store_path / sl_name / original_index_name
assert not old_local_store_path.exists(), f"{old_local_store_path} survived the rename"

new_local_store_path = config2.local_store_path / sl_name / new_index_name
assert new_local_store_path.exists(), f"{new_local_store_path} was not created"

# %% [markdown]
# ## And `doctor` sees no duplicate box id

# %%
#|export
from boxyard.cmds._doctor import run_doctor

report = await run_doctor(config_path=config_path2, check_remote=False)
_dupes = report["checks"]["duplicate-box-id"]["findings"]
assert not _dupes, f"doctor reports duplicate box ids: {_dupes}"

# %%
print("rename propagation OK")

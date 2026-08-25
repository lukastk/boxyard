# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `sync -c data` Must Still Sync CONF
#
# `conf/.rclone_include|_exclude|_filters` decide **what** a box's DATA syncs,
# and `sync_box` reads them off the local disk right before syncing DATA. So a
# DATA sync on a machine that has never pulled this box's `conf/` used the
# GLOBAL filters instead — and a box whose `.rclone_include` narrows what it
# syncs would sync **everything**.
#
# That is exactly the harm v0.5.3 fixed, still reachable through
# `boxyard sync -c data` — and through `boxyard multi-sync -c data`, i.e.
# across the whole yard at once.
#
# META already had this treatment (it is synced whenever DATA is, because the
# ownership decision reads `write_owner` out of it). CONF now does too, for the
# same reason and with the same rule: its result is only RECORDED when CONF was
# actually requested, so the returned dict still answers exactly what the
# caller asked about.

# %%
#|default_exp integration.cmds.test_conf_follows_data
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import pytest
import asyncio

from boxyard import const
from boxyard.cmds import new_box, sync_box, sync_missing_boxmetas, include_box
from boxyard._models import get_boxyard_meta, BoxPart

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_conf_follows_data():
    """`sync -c data` pulls the filters that decide what DATA syncs."""
    asyncio.run(_test_conf_follows_data())

# %%
#|set_func_signature
async def _test_conf_follows_data(): ...

# %% [markdown]
# ## Machine 1 creates a box; machine 2 includes it

# %%
#|export
(
    sl_name,
    sl_rclone_path,
    [(config1, config_path1, data_path1), (config2, config_path2, data_path2)],
) = create_boxyards(num_boxyards=2)

index_name = new_box(
    config_path=config_path1, box_name="filtered-data", storage_location=sl_name
)
await sync_box(config_path=config_path1, box_index_name=index_name)

await sync_missing_boxmetas(config_path=config_path2)
await include_box(config_path=config_path2, box_index_name=index_name)
await sync_box(config_path=config_path2, box_index_name=index_name)

bm2 = get_boxyard_meta(config2, force_create=True).by_index_name[index_name]
conf2 = bm2.get_local_part_path(config2, BoxPart.CONF)
exclude2 = conf2 / const.RCLONE_EXCLUDE_FILENAME
assert not exclude2.exists(), "no per-box exclude has been written yet"

# %% [markdown]
# ## Machine 1 adds a per-box exclude; machine 2 syncs DATA only
#
# The filters decide WHAT DATA syncs, and `sync_box` reads them off the local
# disk immediately before syncing DATA. A `-c data` sync that does not fetch
# them first uses the GLOBAL filters — the exact harm v0.5.3 fixed.

# %%
#|export
bm1 = get_boxyard_meta(config1, force_create=True).by_index_name[index_name]
conf1 = bm1.get_local_part_path(config1, BoxPart.CONF)
conf1.mkdir(parents=True, exist_ok=True)
(conf1 / const.RCLONE_EXCLUDE_FILENAME).write_text("secrets/\n")
await sync_box(config_path=config_path1, box_index_name=index_name)

# TESTREF: test_conf_follows_data
_res = await sync_box(
    config_path=config_path2,
    box_index_name=index_name,
    sync_choices=[BoxPart.DATA],
)

assert exclude2.exists(), (
    "`sync -c data` did not fetch the box's conf/, so DATA was synced with the "
    "GLOBAL filters instead of the box's own"
)
assert exclude2.read_text() == "secrets/\n"

# %% [markdown]
# ## The returned dict still answers only what was asked

# %%
#|export
assert set(_res) == {BoxPart.DATA}, (
    f"`sync -c data` reported parts the caller did not ask about: {set(_res)}"
)

# %%
print("conf follows data OK")

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # A Box's `conf/` Must Reach a Second Machine
#
# `conf/.rclone_include|_exclude|_filters` decide what a box's DATA syncs. If
# they only ever exist on the machine that wrote them, then every OTHER machine
# syncs that box with the wrong filters — and a box whose `.rclone_include`
# narrows its sync would sync EVERYTHING on the second machine.
#
# The cause was a single branch in `get_sync_status`: absent locally + present
# remotely was read as `EXCLUDED`. That is right for DATA, where absence means
# the box is deliberately not included here. For CONF nobody chose anything —
# the files have simply never been fetched — and reading it as `EXCLUDED` made
# the absence **self-perpetuating**: conf/ is missing, so it is judged excluded,
# so it is never pulled, so it stays missing.

# %%
#|default_exp integration.cmds.test_conf_reaches_second_machine
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import pytest
import asyncio

from boxyard import const
from boxyard.cmds import (
    new_box, sync_box, sync_missing_boxmetas, include_box, exclude_box,
)
from boxyard._models import get_boxyard_meta, BoxPart, SyncCondition

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_conf_reaches_a_second_machine():
    """A box's per-box rclone filters arrive on machines that did not write them."""
    asyncio.run(_test_conf_reaches_a_second_machine())

# %%
#|set_func_signature
async def _test_conf_reaches_a_second_machine(): ...

# %% [markdown]
# ## Machine 1 creates a box and gives it a per-box exclude

# %%
#|export
(
    sl_name,
    sl_rclone_path,
    [(config1, config_path1, data_path1), (config2, config_path2, data_path2)],
) = create_boxyards(num_boxyards=2)

index_name = new_box(
    config_path=config_path1, box_name="filtered", storage_location=sl_name
)
bm1 = get_boxyard_meta(config1, force_create=True).by_index_name[index_name]

conf1 = bm1.get_local_part_path(config1, BoxPart.CONF)
conf1.mkdir(parents=True, exist_ok=True)
(conf1 / const.RCLONE_EXCLUDE_FILENAME).write_text("secrets/\n*.log\n")

await sync_box(config_path=config_path1, box_index_name=index_name)

# %% [markdown]
# ## Machine 2 includes the box — and must receive the filters
#
# This is the case the bug made impossible. Machine 2 did not write the conf,
# so its local `conf/` starts absent.

# %%
#|export
await sync_missing_boxmetas(config_path=config_path2)
await include_box(config_path=config_path2, box_index_name=index_name)
await sync_box(config_path=config_path2, box_index_name=index_name)

bm2 = get_boxyard_meta(config2, force_create=True).by_index_name[index_name]
conf2 = bm2.get_local_part_path(config2, BoxPart.CONF)
exclude2 = conf2 / const.RCLONE_EXCLUDE_FILENAME

assert exclude2.exists(), (
    "the box's conf/ never reached the second machine, so its rclone filters "
    "apply only where they were written"
)
assert exclude2.read_text() == "secrets/\n*.log\n", (
    f"conf arrived but its contents differ: {exclude2.read_text()!r}"
)

# %% [markdown]
# ## CONF must report SYNCED afterwards, not EXCLUDED

# %%
#|export
_res = await sync_box(config_path=config_path2, box_index_name=index_name)
_conf_condition = _res[BoxPart.CONF][0].sync_condition
assert _conf_condition == SyncCondition.SYNCED, (
    f"after pulling, CONF still reports {_conf_condition} on the second machine"
)

# %% [markdown]
# ## DATA keeps its meaning: excluding a box must NOT re-pull it
#
# The fix must not leak into DATA. `boxyard exclude` is a deliberate choice, and
# absence there really does mean "not wanted here" — if this regressed, an
# excluded box would quietly come back on the next sync.

# %%
#|export
await exclude_box(config_path=config_path2, box_index_name=index_name)
_res = await sync_box(config_path=config_path2, box_index_name=index_name)
_data_condition = _res[BoxPart.DATA][0].sync_condition
assert _data_condition == SyncCondition.EXCLUDED, (
    f"an excluded box's DATA reported {_data_condition}, not EXCLUDED — "
    "excluding a box no longer sticks"
)
assert not bm2.get_local_part_path(config2, BoxPart.DATA).exists(), (
    "an excluded box's DATA was pulled back onto the machine"
)

# %%
print("conf reaches the second machine OK")

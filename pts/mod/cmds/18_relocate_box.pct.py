# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Relocate a machine-local checkout

# %%
#|default_exp cmds._relocate_box
#|export_as_func true

# %%
#|hide
from nblite import nbl_export; nbl_export();

# %%
#|top_export
from pathlib import Path
from typing import Callable

from boxyard.config import get_config
from boxyard._utils.locking import BoxyardLockManager

# %%
#|set_func_signature
def relocate_box(
    config_path: Path,
    box_index_name: str,
    destination_root: str | None = None,
    adopt_existing: bool = False,
    _phase_hook: Callable[[str], None] | None = None,
) -> Path:
    """Move an included box between checkout roots without contacting remote storage.

    ``adopt_existing`` accepts a pre-populated destination only after proving every
    source entry is present there identically; destination-only content is preserved.
    If a relocation was interrupted, omit ``destination_root`` to recover its
    recorded destination, or provide that same destination explicitly.
    ``_phase_hook`` is a test-only crash-injection seam.
    """
    ...

# %%
#|export
config = get_config(config_path)
from boxyard._models import get_boxyard_meta
from boxyard._checkout import load_placement, PlacementState, relocate_box as _relocate

boxyard_meta = get_boxyard_meta(config)
if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' does not exist.")
box_meta = boxyard_meta.by_index_name[box_index_name]
placement = load_placement(config, box_meta)
if placement.state == PlacementState.RELOCATING:
    assert placement.relocation is not None
    recorded_destination = placement.relocation.destination_root
    if destination_root is None:
        destination_root = recorded_destination
    elif destination_root != recorded_destination:
        raise ValueError(
            f"Interrupted relocation is bound for '{recorded_destination}', not '{destination_root}'. "
            "Recover it before starting another relocation."
        )
elif destination_root is None:
    raise ValueError("--checkout-root is required when starting a relocation")

# %%
#|export
lock_manager = BoxyardLockManager(config.boxyard_data_path)
# All sync/include/exclude/delete/rename operations take the per-box lock. The
# global lock then serializes the machine-local placement commit and derived
# links with cache/global-state updates.
with lock_manager.box_sync_lock(box_index_name):
    with lock_manager.global_lock():
        result = _relocate(
            config,
            box_meta,
            destination_root,
            adopt_existing=adopt_existing,
            phase_hook=_phase_hook,
        )

# %%
#|func_return
result;

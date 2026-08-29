# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _claim_box
#
# `boxyard claim` — make this machine the single writer of a box's DATA.
#
# Three things about this command are not obvious and are all deliberate:
#
# 1. **It verifies by reading the remote back.** Two machines claiming at the
#    same instant is plain last-write-wins — measured at 5 trials in 6 — and the
#    loser reverts SILENTLY, because a completed push writes a fresh sync record
#    so its own claim then looks like an ordinary `needs_pull`. A `claim` that
#    printed "ok" and did not stick would be worse than no command at all, so
#    after pushing META we re-read the REMOTE boxmeta and fail loudly if it does
#    not name this machine. This shrinks the window; it does not close it.
#    Ownership converges — it is not a lock and is not linearizable.
#
# 2. **It refuses a box that is not included here.** A box that is not included
#    still has a local registration and `boxmeta.toml` — that is exactly what
#    `sync-missing-meta` maintains for the hundreds of boxes a machine does not
#    hold — so without this check the claim would succeed and make this machine
#    the write owner of a box whose DATA it does not have. Every machine that
#    DOES have it is then locked out, and the only way back is `--steal` from
#    one of them. Ownership means "I am the machine that may push this box's
#    DATA"; you cannot push DATA you do not hold.
#
# 3. **`--steal` is a separate, loud act.** Taking a box from a live machine
#    means that machine's unpushed work will be refused there from then on, so
#    it says so in those words and requires confirmation.

# %%
#|default_exp cmds._claim_box
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path

from boxyard import const
from boxyard._models import BoxMeta, BoxPart, get_boxyard_meta, refresh_boxyard_meta
from boxyard._ownership import OwnershipRefused, require_machine_name
from boxyard._remote_index import find_remote_box_by_id
from boxyard._utils import rclone_cat
from boxyard._utils.sync_helper import SyncSetting
from boxyard.config import get_config, StorageType

# %%
#|set_func_signature
async def claim_box(
    config_path: Path,
    box_index_name: str,
    steal: bool = False,
    verbose: bool = True,
) -> str:
    """
    Make this machine the write owner of a box.

    Args:
        config_path: Path to the boxyard config file.
        box_index_name: Index name of the box to claim.
        steal: Take the box from another machine that currently owns it.
            Without this, an already-owned box is refused.
        verbose: Print what happened.

    Returns:
        The machine name now recorded as the box's `write_owner`.

    Raises:
        OwnershipRefused: if this machine has no `machine_name`, if the box is
            not included here, or if another machine owns it and `steal` is not
            set.
    """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_boxyards
from boxyard.cmds import new_box, sync_box

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()
box_index_name = new_box(
    config_path=config_path, box_name="claim-me", storage_location=remote_name
)
await sync_box(config_path=config_path, box_index_name=box_index_name)
steal = False
verbose = True

# %% [markdown]
# # Function body

# %%
#|export
config = get_config(config_path)
machine_name = require_machine_name(config, f"claim '{box_index_name}'")

boxyard_meta = get_boxyard_meta(config)
if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' not found.")

box_meta = boxyard_meta.by_index_name[box_index_name]

if box_meta.get_storage_location_config(config).storage_type == StorageType.LOCAL:
    raise OwnershipRefused(
        f"Cannot claim '{box_index_name}': it is in the local storage location "
        f"'{box_meta.storage_location}', which no other machine can reach, so "
        f"there is nothing to coordinate."
    )

# %% [markdown]
# ## Refuse a box this machine does not hold
#
# See point 2 in the module docstring. The message names the exact command that
# fixes it, because a refusal that does not is a refusal people work around.

# %%
#|export
from boxyard._checkout import get_box_checkout_status, LocalCheckoutState

_checkout = get_box_checkout_status(config, box_meta)
if _checkout.state == LocalCheckoutState.UNAVAILABLE:
    raise OwnershipRefused(
        f"Cannot claim '{box_index_name}': it is included in unavailable checkout "
        f"root '{_checkout.checkout_root}'. Restore that root first; Boxyard will "
        "not treat it as excluded or fall back."
    )
if _checkout.state != LocalCheckoutState.INCLUDED:
    raise OwnershipRefused(
        f"Cannot claim '{box_index_name}': it is not included on this machine "
        f"as a complete checkout (state: {_checkout.state.value}), "
        f"so this machine has no complete DATA to push. Include/recover it first: "
        f"`boxyard include -r '{box_index_name}'`, then claim it."
    )

# %% [markdown]
# ## Bring META up to date before deciding anything
#
# Both the decision below and the push that follows it depend on this machine's
# boxmeta being current. Without it the sequence fails in an avoidable and very
# confusing way: META is writable by EVERY machine (it has to be, or ownership
# could never be transferred), so a non-owner that merely pulled the box will
# have written a fresh META sync record of its own. Editing a stale local
# boxmeta and pushing then reads as `conflict` — both sides moved — and the
# command refuses for a reason that has nothing to do with ownership.
#
# Pulling first also means the ownership decision is made against what the
# fleet last said, rather than against whatever this machine happened to know.
# A genuine META conflict still raises here, which is right: a person typed
# this command and is waiting for an answer.

# %%
#|export
from boxyard.cmds import sync_box

await sync_box(
    config_path=config_path,
    box_index_name=box_index_name,
    sync_choices=[BoxPart.META],
    sync_setting=SyncSetting.CAREFUL,
    verbose=False,
)

# %% [markdown]
# ## Refuse a box another machine owns, unless stealing

# %%
#|export
# Re-read from disk rather than trusting the cache: `boxyard_meta.json` is a
# snapshot of the last refresh, and a META pull since then is exactly how
# another machine's claim arrives here.
box_meta = BoxMeta.load(config, box_meta.storage_location, box_index_name)
previous_owner = box_meta.write_owner

if previous_owner == machine_name:
    if verbose:
        print(f"'{box_index_name}' is already owned by this machine ({machine_name}).")
    machine_name  #|func_return_line

if previous_owner is not None and not steal:
    raise OwnershipRefused(
        f"Cannot claim '{box_index_name}': it is owned by '{previous_owner}'.\n"
        f"The tidy handover is `boxyard release -r '{box_index_name}'` on "
        f"'{previous_owner}', then `boxyard claim -r '{box_index_name}'` here. "
        f"If that machine is gone or unreachable, take it over with "
        f"`boxyard claim --steal -r '{box_index_name}'`."
    )

# %% [markdown]
# ## Write, push, and verify by reading the remote back
#
# The read-back is the whole reason this is a command and not a boxmeta edit.
# See point 1 in the module docstring.

# %%
#|export
box_meta.write_owner = machine_name
box_meta.save(config)

await sync_box(
    config_path=config_path,
    box_index_name=box_index_name,
    sync_choices=[BoxPart.META],
    sync_setting=SyncSetting.CAREFUL,
    verbose=False,
)

# %%
#|export
_remote_index_name = await find_remote_box_by_id(
    config, box_meta.storage_location, box_meta.box_id
)
if _remote_index_name is None:
    _remote_index_name = box_index_name

_remote_boxmeta_path = (
    config.storage_locations[box_meta.storage_location].store_path
    / const.REMOTE_BOXES_REL_PATH
    / _remote_index_name
    / const.BOX_METAFILE_REL_PATH
)
_exists, _raw = await rclone_cat(
    rclone_config_path=config.rclone_config_path,
    source=box_meta.storage_location,
    source_path=_remote_boxmeta_path.as_posix(),
)

# A claim that did not reach the remote is not a claim. Say so loudly rather
# than leaving this machine believing it owns a box the fleet disagrees about
# -- the loser of a concurrent claim otherwise reverts with no message at all.
if not _exists:
    raise OwnershipRefused(
        f"Claimed '{box_index_name}' locally, but could not read the boxmeta "
        f"back from '{box_meta.storage_location}' to confirm it. Do not rely on "
        f"this claim; re-run `boxyard claim -r '{box_index_name}'` once the "
        f"remote is reachable."
    )

import tomllib as _tomllib

_remote_owner = _tomllib.loads(_raw).get("write_owner")
if _remote_owner != machine_name:
    raise OwnershipRefused(
        f"Claim of '{box_index_name}' did not stick: the remote boxmeta now "
        f"names '{_remote_owner}', not '{machine_name}'. Another machine "
        f"claimed it at the same moment and won — claiming converges on one "
        f"owner but is not atomic.\n"
        f"This machine's local boxmeta will revert on the next sync. If you "
        f"want the box anyway, run "
        f"`boxyard claim --steal -r '{box_index_name}'`."
    )

refresh_boxyard_meta(config)

if verbose:
    if previous_owner is None:
        print(f"Claimed '{box_index_name}' for '{machine_name}'.")
    else:
        print(
            f"Took '{box_index_name}' from '{previous_owner}'; it is now owned by "
            f"'{machine_name}'.\n"
            f"Any unpushed work for this box on '{previous_owner}' will be refused "
            f"there from now on, and must be recovered with "
            f"`boxyard discard-local` or by claiming it back."
        )

# %%
#|func_return
machine_name

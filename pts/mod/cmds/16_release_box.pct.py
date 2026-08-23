# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _release_box
#
# `boxyard release` — give up write ownership of a box, returning it to the
# unowned (= unrestricted) state.
#
# The clean half of a handover: `release` on the machine that has it, then
# `claim` on the machine that wants it. Two online steps, no force, no race —
# as opposed to `claim --steal`, which exists for when the owner is gone.
#
# Because `BoxMeta.save` omits `write_owner` when it is `None`, releasing
# returns `boxmeta.toml` to exactly its pre-v0.5 bytes.

# %%
#|default_exp cmds._release_box
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path

from boxyard import const
from boxyard._models import BoxMeta, BoxPart, get_boxyard_meta, refresh_boxyard_meta
from boxyard._ownership import OwnershipRefused
from boxyard._remote_index import find_remote_box_by_id
from boxyard._utils import rclone_cat
from boxyard._utils.sync_helper import SyncSetting
from boxyard.config import get_config


async def _remote_owner_is_cleared(config, box_meta, box_index_name: str) -> bool:
    """Does the REMOTE boxmeta now show this box as unowned?"""
    import tomllib

    remote_index_name = await find_remote_box_by_id(
        config, box_meta.storage_location, box_meta.box_id
    )
    if remote_index_name is None:
        remote_index_name = box_index_name
    remote_path = (
        config.storage_locations[box_meta.storage_location].store_path
        / const.REMOTE_BOXES_REL_PATH
        / remote_index_name
        / const.BOX_METAFILE_REL_PATH
    )
    exists, raw = await rclone_cat(
        rclone_config_path=config.rclone_config_path,
        source=box_meta.storage_location,
        source_path=remote_path.as_posix(),
    )
    if not exists:
        return False
    return tomllib.loads(raw).get("write_owner") is None

# %%
#|set_func_signature
async def release_box(
    config_path: Path,
    box_index_name: str,
    verbose: bool = True,
    _skip_sync: bool = False,
) -> None:
    """
    Give up this machine's write ownership of a box.

    Args:
        config_path: Path to the boxyard config file.
        box_index_name: Index name of the box to release.
        verbose: Print what happened.
        _skip_sync: Write the boxmeta but do not push it. Only for callers that
            are already inside a sync of this box; a release that is not pushed
            is invisible to every other machine.

    Raises:
        OwnershipRefused: if the box is owned by a different machine.
    """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_boxyards
from boxyard.cmds import claim_box, new_box, sync_box

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()
box_index_name = new_box(
    config_path=config_path, box_name="release-me", storage_location=remote_name
)
await sync_box(config_path=config_path, box_index_name=box_index_name)
await claim_box(config_path=config_path, box_index_name=box_index_name)
verbose = True
_skip_sync = False

# %% [markdown]
# # Function body

# %%
#|export
config = get_config(config_path)

boxyard_meta = get_boxyard_meta(config)
if box_index_name not in boxyard_meta.by_index_name:
    raise ValueError(f"Box '{box_index_name}' not found.")

# Re-read from disk: the cache predates any META pull that carried another
# machine's claim.
box_meta = BoxMeta.load(
    config, boxyard_meta.by_index_name[box_index_name].storage_location, box_index_name
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

# %%
#|export
box_meta = BoxMeta.load(config, box_meta.storage_location, box_index_name)

if box_meta.write_owner is None:
    if verbose:
        print(f"'{box_index_name}' is already unowned; nothing to release.")
    None  #|func_return_line

if box_meta.write_owner != config.machine_name:
    raise OwnershipRefused(
        f"Cannot release '{box_index_name}': it is owned by "
        f"'{box_meta.write_owner}', not by this machine "
        f"({config.machine_name or 'unnamed'}).\n"
        f"Run `boxyard release -r '{box_index_name}'` on "
        f"'{box_meta.write_owner}', or take the box over here with "
        f"`boxyard claim --steal -r '{box_index_name}'`."
    )

# %% [markdown]
# ## Write, push, and verify — rolling back if it did not land
#
# A release that only happened locally is worse than no release: every other
# machine still believes this one owns the box, while this one believes it is
# free. So the local boxmeta is restored if the push does not reach the remote,
# and the caller gets an exception rather than a false success.
#
# `exclude` depends on exactly this: it refuses to drop a box it owns when the
# release cannot be published, and that refusal is only honest if the local
# boxmeta is unchanged afterwards.

# %%
#|export
_previous_owner = box_meta.write_owner
box_meta.write_owner = None
box_meta.save(config)

if not _skip_sync:
    try:
        await sync_box(
            config_path=config_path,
            box_index_name=box_index_name,
            sync_choices=[BoxPart.META],
            sync_setting=SyncSetting.CAREFUL,
            verbose=False,
        )
        _landed = await _remote_owner_is_cleared(config, box_meta, box_index_name)
    except Exception:
        box_meta.write_owner = _previous_owner
        box_meta.save(config)
        raise

    if not _landed:
        box_meta.write_owner = _previous_owner
        box_meta.save(config)
        raise OwnershipRefused(
            f"Could not publish the release of '{box_index_name}': the remote "
            f"boxmeta does not show it as unowned, so other machines would "
            f"still treat '{_previous_owner}' as the write owner. This machine "
            f"still owns it; try again once the remote is reachable."
        )

refresh_boxyard_meta(config)

if verbose:
    print(
        f"Released '{box_index_name}'. It is unowned again, so any machine may "
        f"push it — claim it elsewhere with `boxyard claim -r '{box_index_name}'`."
    )

# %%
assert BoxMeta.load(config, remote_name, box_index_name).write_owner is None

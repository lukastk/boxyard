# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _ownership
#
# Single-writer box ownership: a box may be included on any number of machines,
# but exactly one machine at a time may PUSH its DATA.
#
# The whole model is three rules:
#
# 1. **Unowned means unrestricted.** A box with no `write_owner` behaves exactly
#    as it did before v0.5 — ownership is opt-in per box, and a box nobody
#    claimed is nobody's to refuse. Anything else would mass-assign state to
#    hundreds of boxes nobody chose.
# 2. **Ownership is checked before, and independently of, `sync_setting`.**
#    `--sync-setting force` is a *sync-safety* override ("I accept overwriting");
#    ownership is a *coordination* statement ("another machine is the writer").
#    Conflating them means the muscle-memory `force` used to unstick a box
#    silently steals ownership and, worse, leaves the remote holding this
#    machine's data while `boxmeta.toml` still names another as owner — a lie in
#    shared state, which is strictly worse than a refusal. There is deliberately
#    no `--ignore-ownership` flag for the same reason.
# 3. **The routine refusal is not an exception.** See
#    `SyncCondition.WRITE_DENIED`. Only *deliberate* commands (`claim`,
#    `force-push`, `rename --scope remote`, `delete`, `exclude`) raise, because a
#    person typed them and is waiting for an answer.
#
# It is not a lock and does not pretend to be. Two machines claiming at the same
# instant is last-write-wins — measured at 5 times in 6 — so `claim` verifies by
# reading the remote back, and CONFLICT detection stays load-bearing.

# %%
#|default_exp _ownership

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
from pathlib import Path

import boxyard.config
from boxyard._models import BoxMeta

# %% [markdown]
# ## Refusals

# %%
#|export
class OwnershipRefused(Exception):
    """
    A deliberate, user-initiated action was refused because this machine is not
    the box's write owner (or has no machine name to be one with).

    Only ever raised out of a command a person typed. The routine sync path uses
    `SyncCondition.WRITE_DENIED` instead — see this module's docstring.
    """

# %% [markdown]
# ## Who may write
#
# `machine_name` unset is treated as "not the owner", never as "the owner": a
# machine that cannot say who it is must not be able to claim it is the writer.
# That is the safe direction, and it costs nothing while a box is unowned.

# %%
#|export
def may_push(config: boxyard.config.Config, box_meta: BoxMeta) -> bool:
    """Is this machine allowed to push `box_meta`'s DATA/CONF?"""
    if box_meta.write_owner is None:
        return True  # unowned == unrestricted
    return box_meta.write_owner == config.machine_name


def require_machine_name(config: boxyard.config.Config, action: str) -> str:
    """
    Return this machine's name, or refuse with a message that names the fix.

    Ownership identifies a machine by a configured name and never by its
    hostname: one machine in this fleet has reported both `lukas-pocket4` and
    `pocket4`, and macOS reports user-editable pretty names.
    """
    if not config.machine_name:
        raise OwnershipRefused(
            f"Cannot {action}: this machine has no name, so it cannot be "
            f"recorded as a box's write owner.\n"
            f"Set `machine_name` in '{config.config_path}' to this machine's "
            f"canonical short name (e.g. 'macbook' or 'mymain'), then try again."
        )
    return config.machine_name

# %% [markdown]
# ## The refusal text
#
# Every hint here names an exact command that is safe to run verbatim. That is a
# rule with a scar behind it: `doctor`'s `duplicate-box-id` hint used to say
# "delete or re-create one of them", and `delete` purges the remote and writes a
# tombstone keyed by box id — so following it destroys BOTH boxes.
#
# There are exactly two ways out of `WRITE_DENIED`, and both are always named:
# take the box over, or throw the local changes away. A refusal with only one
# escape is a refusal people work around.

# %%
#|export
def write_denied_message(
    config: boxyard.config.Config,
    box_meta: BoxMeta,
    part_label: str = "DATA",
) -> str:
    """The one-paragraph explanation shared by sync, doctor and multi-sync."""
    return (
        f"'{box_meta.index_name}' is owned by '{box_meta.write_owner}', so the "
        f"{part_label} of this copy is not pushed. It still pulls."
    )


def write_denied_hint(
    config: boxyard.config.Config,
    box_meta: BoxMeta,
) -> str:
    """The two ways out, both safe to run exactly as written."""
    return (
        f"Either take the box over with `boxyard claim --steal -r "
        f"'{box_meta.index_name}'`, or throw away this machine's changes with "
        f"`boxyard discard-local -r '{box_meta.index_name}'` (which keeps a "
        f"copy under '{config.local_sync_backups_path}'). "
        f"The tidy handover is `boxyard release` on '{box_meta.write_owner}' "
        f"followed by `boxyard claim` here."
    )


def owner_gate(
    config: boxyard.config.Config,
    box_meta: BoxMeta,
    action: str,
) -> None:
    """
    Refuse a deliberate remote-mutating action on a box another machine owns.

    Used by the paths that bypass `sync_helper` entirely and would otherwise
    write to the remote with no ownership check at all: `force-push`, `rename
    --scope remote|both` (it renames the remote directory), and `delete` (it
    purges the remote and writes a tombstone).
    """
    if may_push(config, box_meta):
        return
    raise OwnershipRefused(
        f"Cannot {action}: {write_denied_message(config, box_meta)}\n"
        f"{write_denied_hint(config, box_meta)}"
    )

# %% [markdown]
# ## The dry-run probe
#
# Mandatory, not an optimisation. Measured: creating `.DS_Store`,
# `__pycache__/x.pyc` or `.venv/pyvenv.cfg` flips a box to `needs_push` even
# though all three are in `DEFAULT_RCLONE_EXCLUDE` and the resulting push
# transfers nothing — `get_sync_status` asks `check_last_time_modified`, which
# is a tree walk, not a filter-aware one. Without this probe every read-only
# machine would report "you have local changes" forever, for changes that do not
# exist, and the feature would be unusable.
#
# (The deeper fix is to make `check_last_time_modified` filter-aware, which would
# also remove today's yard-wide no-op pushes. That is a separate, riskier change
# — getting rclone's filter semantics subtly wrong means silently not syncing
# real work — and is deliberately not attempted here.)

# %%
#|export
async def push_would_transfer(
    config: boxyard.config.Config,
    *,
    local_path: Path,
    remote: str,
    remote_path: Path,
    include_path: Path | None = None,
    exclude_path: Path | None = None,
    filters_path: Path | None = None,
) -> bool:
    """
    Would pushing `local_path` to `remote:remote_path` actually change anything?

    Uses the box's real filters, so debris that can never be transferred does
    not count as a change.

    Returns True when the answer is yes AND when the question could not be
    answered at all. Both callers of this treat True as "refuse to push and
    report it", so an unreachable remote surfaces as a reported state rather
    than as a silent all-clear — the one thing this must never do is claim a
    box is clean because it failed to look.
    """
    from boxyard._utils import rclone_check

    answered, differing = await rclone_check(
        rclone_config_path=config.rclone_config_path,
        source="",
        source_path=local_path.as_posix(),
        dest=remote,
        dest_path=remote_path.as_posix(),
        include_file=include_path.as_posix() if include_path else None,
        exclude_file=exclude_path.as_posix() if exclude_path else None,
        filters_file=filters_path.as_posix() if filters_path else None,
    )
    if not answered:
        return True
    return bool(differing)

# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _sync_policy
#
# How often each part of each box is checked, and whether its DATA is stored
# packed. See `_dev/SYNC-CADENCE-DESIGN-NOTE.md` for the reasoning; this module
# is the mechanism.
#
# Three rules carry the whole design:
#
# 1. **An absent policy configuration changes nothing.** A `config.toml` with no
#    `[sync_policies.*]` tables makes `due_boxes` return every box, every time --
#    exactly today's behaviour, where a supervisor loop syncs everything on a
#    fixed sleep. The feature is opt-in per fleet, and a machine that has not
#    opted in must not quietly start skipping boxes.
# 2. **Resolution is per DIMENSION, not per policy.** A box takes its DATA
#    cadence from `conf/sync.toml` and its META cadence from the group policy if
#    that is what each level states, so a box never has to restate a setting it
#    did not want to change.
# 3. **Ambiguity is refused, never joined.** A box matching two policies that
#    disagree on one dimension is an error a person settles, reported by
#    `doctor`. The alternatives were both worse: a global precedence list is a
#    hand-maintained ordering that silently changes existing boxes whenever a
#    group is added, and a most-conservative-wins join has no correct direction
#    for an interval -- "shortest wins" defeats an archive schedule, "longest
#    wins" means any slow group silently slows a box.
#
# On the state this module keeps: `due_boxes` needs "when did we last CHECK this
# box", which is NOT what the sync records hold. A `.rec` timestamp is the last
# TRANSFER, so scheduling on it would make an unchanged box permanently overdue
# and check it every tick -- precisely the cost the cadence exists to avoid. So
# a check record is written per (box, part) under `sync_checks/`.
#
# That state degrades in ONE direction on purpose: a check record that is
# missing, unreadable or malformed means "due now" and "assume changed", never
# "up to date". Losing it costs work, never correctness, and the directory can
# be deleted at any time to force a full pass.

# %%
#|default_exp _sync_policy

# %%
#|export
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boxyard.config
from boxyard import const
from boxyard._models import BoxMeta
from boxyard._enums import BoxPart, StorageFormat

# %%
#|export
SYNC_CHECKS_REL_PATH = "sync_checks"

BOX_SYNC_CONF_FILENAME = "sync.toml"

# The parts a cadence can be set for. CONF deliberately has none: it is tiny,
# it rides the DATA sync that needs it (its rclone filters are read before the
# DATA transfer), and a separate cadence for it would be a knob with no
# question behind it.
SCHEDULABLE_PARTS = (BoxPart.DATA, BoxPart.META)


class PolicyConflict(Exception):
    """
    A box matches two policies that state DIFFERENT values for one dimension.

    Carries the box, the dimension and the competing policies so the message
    names what to fix rather than only that something is wrong.
    """

    def __init__(
        self, box_index_name: str, dimension: str, choices: dict[str, Any]
    ):
        self.box_index_name = box_index_name
        self.dimension = dimension
        self.choices = choices
        rendered = ", ".join(
            f"{name}={value!r}" for name, value in sorted(choices.items())
        )
        super().__init__(
            f"Box '{box_index_name}' matches policies that disagree on "
            f"'{dimension}': {rendered}. Set '{dimension}' in the box's own "
            f"conf/{BOX_SYNC_CONF_FILENAME} to settle it, or stop the box "
            f"matching more than one of these policies."
        )


@dataclass(frozen=True)
class ResolvedPolicy:
    """
    The effective settings for one box, plus where each came from.

    `sources` exists so `doctor` and `--explain` can answer "why is this box on
    a 7-day cadence" without the user reverse-engineering the resolution order.
    """

    data_interval_seconds: int | None
    meta_interval_seconds: int | None
    # The format this box SHOULD have. NOT what it has -- that is
    # `BoxMeta.storage_format`, and only `boxyard convert` changes it. Nothing
    # acts on this yet; `doctor` reports where the two differ.
    storage_format: StorageFormat = StorageFormat.PLAIN
    sources: dict[str, str] = field(default_factory=dict)

    def interval_seconds(self, part: BoxPart) -> int | None:
        if part == BoxPart.DATA:
            return self.data_interval_seconds
        if part == BoxPart.META:
            return self.meta_interval_seconds
        raise ValueError(f"{part} has no cadence; schedulable parts are {SCHEDULABLE_PARTS}")


# The settings a box may override in its own conf/, and the policy field each
# maps to. Kept explicit rather than derived from SyncPolicyConfig because
# `groups` is a policy-level concept that a single box must not be able to set.
BOX_OVERRIDABLE = ("data_interval", "meta_interval", "storage_format")

# The format a box gets when no policy says otherwise. It is `plain` until the
# restic backend is switched on fleet-wide -- the last step of the rollout, not
# the first. See the design note's "restic as the default, and how the switch is
# gated".
DEFAULT_STORAGE_FORMAT = StorageFormat.PLAIN

# %%
#|export
def read_box_sync_override(
    config: boxyard.config.Config, box_meta: BoxMeta
) -> dict[str, Any]:
    """
    Read a box's own `conf/sync.toml`, or `{}` if it has none.

    A box without the file is the normal case, not a problem -- almost no box
    will have one. A box WITH an unparseable one is a loud failure: it was
    written deliberately, so silently ignoring it would apply a cadence the
    author did not ask for and never say so.
    """
    import tomllib

    path = (
        box_meta.get_local_part_path(config, BoxPart.CONF)
        / BOX_SYNC_CONF_FILENAME
    )
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            parsed = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: not valid TOML: {exc}") from exc

    unknown = set(parsed) - set(BOX_OVERRIDABLE)
    if unknown:
        raise ValueError(
            f"{path}: unknown key(s) {sorted(unknown)}. A box may set "
            f"{list(BOX_OVERRIDABLE)}; 'groups' is a policy-level concept and "
            f"cannot be set per box."
        )
    return parsed


def matching_policies(
    config: boxyard.config.Config, box_meta: BoxMeta
) -> dict[str, "boxyard.config.SyncPolicyConfig"]:
    """
    The named policies whose `groups` intersect this box's groups.

    The `default` policy is NOT included: it is the floor every box falls back
    to, so counting it as a match would make every box that matches any policy
    look ambiguous.
    """
    out = {}
    for name, policy in config.sync_policies.items():
        if name == "default":
            continue
        if set(policy.groups) & set(box_meta.groups):
            out[name] = policy
    return out


def _resolve_dimension(
    box_index_name: str,
    dimension: str,
    override: dict[str, Any],
    matched: dict[str, Any],
    default: Any,
    default_source: str,
) -> tuple[Any, str]:
    """
    Resolve ONE dimension: box override, then matched policies, then default.

    Two matched policies stating the SAME value is not a conflict -- a box in
    both `archived` and `dormant`, where both map to the same cold settings,
    has been asked for one thing twice. Only genuinely different values raise.
    """
    if dimension in override and override[dimension] is not None:
        return override[dimension], f"conf/{BOX_SYNC_CONF_FILENAME}"

    stated = {
        name: value for name, value in matched.items() if value is not None
    }
    distinct = set(stated.values())
    if len(distinct) > 1:
        raise PolicyConflict(box_index_name, dimension, stated)
    if stated:
        name = sorted(stated)[0]
        return stated[name], f"sync_policies.{name}"

    return default, default_source


def resolve_policy(
    config: boxyard.config.Config, box_meta: BoxMeta
) -> ResolvedPolicy:
    """
    The effective sync policy for one box.

    With no `[sync_policies.*]` configured at all this returns intervals of
    `None` -- meaning "no cadence, always due" -- which is what keeps an
    un-opted-in fleet behaving exactly as it does today.
    """
    override = read_box_sync_override(config, box_meta)
    matched = matching_policies(config, box_meta)
    default = config.sync_policies.get("default")

    def _defaults(dimension: str):
        if default is None:
            return None, "unset"
        return getattr(default, dimension), "sync_policies.default"

    sources: dict[str, str] = {}
    resolved: dict[str, Any] = {}
    for dimension in BOX_OVERRIDABLE:
        default_value, default_source = _defaults(dimension)
        value, source = _resolve_dimension(
            box_meta.index_name,
            dimension,
            override,
            {name: getattr(p, dimension) for name, p in matched.items()},
            default_value,
            default_source,
        )
        resolved[dimension] = value
        sources[dimension] = source

    def _seconds(dimension: str) -> int | None:
        raw = resolved[dimension]
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError(
                f"Box '{box_meta.index_name}': {dimension} must be a string "
                f"like '6h' (from {sources[dimension]}); got {raw!r}"
            )
        return boxyard.config.parse_interval(
            raw, f"{sources[dimension]}.{dimension}"
        )

    def _format() -> StorageFormat:
        raw = resolved["storage_format"]
        if raw is None:
            return DEFAULT_STORAGE_FORMAT
        try:
            return StorageFormat(raw)
        except ValueError:
            raise ValueError(
                f"Box '{box_meta.index_name}': storage_format must be one of "
                f"{[f.value for f in StorageFormat]} (from "
                f"{sources['storage_format']}); got {raw!r}"
            ) from None

    return ResolvedPolicy(
        data_interval_seconds=_seconds("data_interval"),
        meta_interval_seconds=_seconds("meta_interval"),
        storage_format=_format(),
        sources=sources,
    )

# %%
#|export
def check_record_path(
    config: boxyard.config.Config, box_index_name: str, part: BoxPart
) -> Path:
    return (
        config.boxyard_data_path
        / SYNC_CHECKS_REL_PATH
        / box_index_name
        / f"{part.value}.json"
    )


def read_check_record(
    config: boxyard.config.Config, box_index_name: str, part: BoxPart
) -> dict[str, Any] | None:
    """
    The record of the last successful CHECK of this (box, part), or None.

    None means "never checked, or the record is unusable" and every caller must
    read it as "do the work". Corruption is deliberately NOT raised: this file
    is a local optimisation, it is regenerated by doing the sync it would have
    skipped, and refusing to sync a box because a cache file got truncated
    would turn a harmless local problem into a stalled box.
    """
    path = check_record_path(config, box_index_name, part)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    if not isinstance(record.get("last_checked_unix"), (int, float)):
        return None
    return record


def write_check_record(
    config: boxyard.config.Config,
    box_index_name: str,
    part: BoxPart,
    now_unix: float,
    remote_modtime: str | None = None,
    remote_size: int | None = None,
) -> Path:
    """
    Record that this (box, part) was successfully checked at `now_unix`.

    `remote_modtime`/`remote_size` are what the bulk listing reported for the
    remote object at that moment. The META skip filter compares them against a
    later listing to decide whether anything moved -- see
    `remote_looks_unchanged`.

    Written via a temp file in the same directory then renamed, so a crash
    mid-write leaves either the old record or the new one, never a truncated
    file that reads as "never checked" and silently costs a full pass.
    """
    import os
    import tempfile

    path = check_record_path(config, box_index_name, part)
    path.parent.mkdir(parents=True, exist_ok=True)

    # A caller with no stamp to offer must not ERASE the one already recorded.
    # An ordinary `multi-sync` pass records only a timestamp, and wiping the
    # stamp would disarm the skip filter every time an unfiltered pass ran --
    # so the filter could never take effect on a machine that also runs the
    # normal DATA pass, which is every machine.
    #
    # Carrying an older stamp forward is the SAFE direction: if the remote moved
    # since, the next listing reports a different ModTime/Size and the box is
    # synced. A stale stamp can only ever cause extra work, never a wrong skip.
    if remote_modtime is None and remote_size is None:
        previous = read_check_record(config, box_index_name, part)
        if previous is not None:
            remote_modtime = previous.get("remote_modtime")
            remote_size = previous.get("remote_size")

    record = {
        "last_checked_unix": now_unix,
        "remote_modtime": remote_modtime,
        "remote_size": remote_size,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(record))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


@dataclass
class DueResult:
    """
    What `due_boxes` found: the boxes to sync, and the boxes it could not
    decide about.

    Conflicts are RETURNED rather than raised. Raising would let one
    misconfigured box halt a whole pass, and dropping it silently would be the
    fallback this codebase forbids. Instead a conflicted box is reported AND
    included in `due` -- syncing it is the safe direction, since the ambiguity
    is only about how OFTEN to sync, never about whether it is allowed.
    """

    due: list[str] = field(default_factory=list)
    conflicts: list[PolicyConflict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def due_boxes(
    config: boxyard.config.Config,
    box_metas: list[BoxMeta],
    part: BoxPart,
    now_unix: float,
) -> DueResult:
    """
    Which boxes are due for a `part` sync at `now_unix`, most overdue first.

    Pure local: reads the check records and the box groups already on disk, and
    makes ZERO remote calls. Measured at 171 ms across 590 boxes, which is what
    lets the scheduling loop wake every 15 minutes without costing anything.

    A box with no cadence -- because no policy configured one -- is ALWAYS due.
    That is what makes an un-opted-in config behave exactly as it does today.
    """
    if part not in SCHEDULABLE_PARTS:
        raise ValueError(
            f"{part} is not schedulable; schedulable parts are {SCHEDULABLE_PARTS}"
        )

    result = DueResult()
    overdue_by: dict[str, float] = {}

    for box_meta in box_metas:
        index_name = box_meta.index_name
        try:
            policy = resolve_policy(config, box_meta)
        except PolicyConflict as conflict:
            result.conflicts.append(conflict)
            result.due.append(index_name)
            overdue_by[index_name] = float("inf")
            continue

        interval = policy.interval_seconds(part)
        if interval is None:
            result.due.append(index_name)
            overdue_by[index_name] = float("inf")
            continue

        record = read_check_record(config, index_name, part)
        if record is None:
            result.due.append(index_name)
            overdue_by[index_name] = float("inf")
            continue

        age = now_unix - float(record["last_checked_unix"])
        if age >= interval:
            result.due.append(index_name)
            overdue_by[index_name] = age - interval
        else:
            result.skipped.append(index_name)

    # Most overdue first. A machine that was off comes back with everything
    # due at once; ordering by overdue-ness means the longest-neglected boxes
    # go first rather than whatever order the registry happened to hold.
    result.due.sort(key=lambda name: (-overdue_by[name], name))
    return result


def remote_looks_unchanged(
    record: dict[str, Any] | None, remote_modtime: str | None, remote_size: int | None
) -> bool:
    """
    Whether the remote object matches what was recorded at the last check.

    Both fields must be present and equal. A record that never captured them
    (an older boxyard wrote it) returns False -- "assume changed" -- so an
    upgrade costs one full pass rather than silently skipping every box.

    This is only ever an optimisation: see the note at the top of this module
    for why a false "changed" is harmless and a false "unchanged" cannot arise.
    """
    if record is None:
        return False
    recorded_modtime = record.get("remote_modtime")
    recorded_size = record.get("remote_size")
    if recorded_modtime is None or recorded_size is None:
        return False
    if remote_modtime is None or remote_size is None:
        return False
    return recorded_modtime == remote_modtime and recorded_size == remote_size

# %% [markdown]
# ## The META skip filter ("B-prime")
#
# The fast META loop's cost is dominated by asking the remote about each box:
# the status probe is 2 remote calls per box per part, measured at 0.67s each,
# so 590 boxes is ~6.6 min at concurrency 2. One BULK listing answers the same
# question for every box at once and already runs in about a minute.
#
# So: ask once, in bulk, which boxes could possibly need work, and put the rest
# aside. Everything that survives goes through the existing `sync_box` META
# path unchanged -- nothing here reimplements a sync.
#
# The two sides are tested differently ON PURPOSE:
#
# - **Remote**: `ModTime` + `Size` from the listing, against what was recorded
#   at the last check. Size matters as much as ModTime, because rclone DOES
#   preserve modification times across a push.
# - **Local**: the on-disk boxmeta compared by CONTENT against `meta.base.toml`,
#   the copy the two sides last agreed on. Content rather than mtime, because
#   the question is "is there an edit to push", and a file rewritten with
#   identical content is not one.

# %%
#|export
def local_meta_differs_from_base(
    config: boxyard.config.Config, box_meta: BoxMeta
) -> bool:
    """
    Whether this machine holds a boxmeta edit the remote has not seen.

    No base means "cannot tell", which is reported as DIFFERS -- the direction
    that costs a sync rather than skipping one. A box that has not synced since
    `meta.base.toml` was introduced simply has no base yet.
    """
    from boxyard._models import read_meta_base

    base = read_meta_base(config, box_meta)
    if base is None:
        return True

    on_disk_path = box_meta.get_local_part_path(config, BoxPart.META)
    if not on_disk_path.exists():
        return True
    try:
        # The identity fields are not IN boxmeta.toml -- they are encoded in the
        # index name -- so they are supplied from the registry entry, which is
        # where the index name came from in the first place.
        on_disk = BoxMeta.load_from_path(
            on_disk_path,
            creation_timestamp_utc=box_meta.creation_timestamp_utc,
            box_subid=box_meta.box_subid,
            name=box_meta.name,
            storage_location=box_meta.storage_location,
        )
    except Exception:
        # Unreadable local boxmeta: let the real sync path deal with it and
        # report properly, rather than silently skipping the box here.
        return True

    return base.model_dump() != on_disk.model_dump()


def meta_boxes_needing_sync(
    config: boxyard.config.Config,
    box_metas: list[BoxMeta],
    remote_listing: dict[str, tuple[str | None, int | None]],
) -> tuple[list[str], list[str]]:
    """
    Split boxes into (needs a META sync, provably does not).

    `remote_listing` maps index name -> (ModTime, Size) from ONE bulk
    `rclone lsjson` over the remote's boxes. A box missing from it is treated as
    needing work: it may be new here, deleted there, or on a storage location
    the listing did not cover, and every one of those wants the real sync path
    to look rather than this filter to decide.

    Skipping is ONLY ever an optimisation. Anything wrongly skipped is caught by
    the next unfiltered pass, since the DATA sync always syncs META too.
    """
    needed: list[str] = []
    skippable: list[str] = []

    for box_meta in box_metas:
        index_name = box_meta.index_name
        remote_modtime, remote_size = remote_listing.get(index_name, (None, None))
        record = read_check_record(config, index_name, BoxPart.META)

        if not remote_looks_unchanged(record, remote_modtime, remote_size):
            needed.append(index_name)
            continue
        if local_meta_differs_from_base(config, box_meta):
            needed.append(index_name)
            continue
        skippable.append(index_name)

    return needed, skippable

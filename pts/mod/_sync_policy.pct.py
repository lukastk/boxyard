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

# The format a NEW box gets when no policy says otherwise.
#
# `restic` for a remote storage location, because that is what the measurements
# argue for and `plain` is now the deliberate exception. `plain` for a `local`
# one: there is no remote, so the per-file transaction cost that motivates the
# whole design does not exist, and a repository would only add a key to lose.
#
# This governs CREATION ONLY. An existing box keeps whatever
# `BoxMeta.storage_format` records until an explicit `boxyard convert` changes
# it, and `doctor` reports the difference. A config edit must never reformat the
# primary copy of everything on the next pass.
DEFAULT_STORAGE_FORMAT = StorageFormat.RESTIC
DEFAULT_STORAGE_FORMAT_LOCAL = StorageFormat.PLAIN


def default_storage_format(
    config: boxyard.config.Config, box_meta: BoxMeta
) -> StorageFormat:
    """The format a box of this kind gets when no policy states one."""
    from boxyard.config import StorageType

    sl_config = config.storage_locations.get(box_meta.storage_location)
    if sl_config is None or sl_config.storage_type == StorageType.LOCAL:
        return DEFAULT_STORAGE_FORMAT_LOCAL
    return DEFAULT_STORAGE_FORMAT

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
            return default_storage_format(config, box_meta)
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
# ## The restic DATA skip filter
#
# The one remaining `(ModTime, Size)` filter: a restic box's remote signal is
# its snapshot POINTER, listed in bulk beside the boxmetas. The stamp it is
# compared against is written from the PRE-pass listing (see `multi-sync`), so a
# push that lands after the listing mismatches next pass -- the loud direction.
# META, CONF and plain DATA are proven by record identity instead, below.

# %%
#|export
def data_boxes_needing_sync(
    config: boxyard.config.Config,
    box_metas: list[BoxMeta],
    remote_listing: dict[str, tuple[str | None, int | None]],
) -> tuple[list[str], list[str]]:
    """
    Split boxes into (needs a DATA sync, provably does not).

    Rides the bulk `boxes/` listing: `boxes/<box>/data.snapshot` sits at depth
    2 beside `boxmeta.toml`, so the listing `multi-sync` already takes answers
    this too.

    Only RESTIC boxes are judged here: a plain box's DATA is proven by record
    identity plus fingerprint in `plain_data_provably_unchanged`, and
    `boxes_needing_sync_full` routes each format to its own predicate. A plain
    box handed to this function is always reported as needed.

    Two conditions, both cheap:

    - the remote pointer has not moved, by (ModTime, Size) against what was
      recorded at the last check;
    - the local tree has not been TOUCHED since this machine last agreed, by
      `tree_touched_since` -- the same gate the restic backend uses to decide
      whether to talk to the repository at all.

    Skipping is ONLY ever an optimisation. A wrong "changed" costs a sync. A
    wrong "unchanged" LOSES DATA, silently, until something else moves.

    IT USED TO USE `tree_modified_since`, AND THAT WAS TWO BUGS ON ONE LINE:

    1. That helper is the plain backend's newest-mtime walk, which sees two of
       ten change shapes. A restic box whose only change was a lone deletion, a
       rename, a chmod or a symlink edit was declared "provably unchanged" and
       skipped -- so it was never backed up, while this docstring claimed a
       wrong "unchanged" was prevented. `tree_touched_since` stats directories
       and uses max(mtime, ctime), which catches all of them; its false
       positives cost the real path one local check, which is precisely the
       trade a gate is allowed to make.
    2. It was called with NO exclude names, so any `.DS_Store` counted as a
       change. On a fleet with Macs in it that likely meant the filter never
       skipped anything at all -- the optimisation silently defeating itself,
       in the harmless direction, which is why nobody noticed.
    """
    from boxyard._enums import StorageFormat
    from boxyard._restic import read_state, tree_touched_since
    from boxyard._utils import literal_exclude_names

    needed: list[str] = []
    skippable: list[str] = []

    for box_meta in box_metas:
        index_name = box_meta.index_name
        if box_meta.storage_format is not StorageFormat.RESTIC:
            needed.append(index_name)
            continue

        remote_modtime, remote_size = remote_listing.get(index_name, (None, None))
        record = read_check_record(config, index_name, BoxPart.DATA)
        if not remote_looks_unchanged(record, remote_modtime, remote_size):
            needed.append(index_name)
            continue

        state = read_state(config.boxyard_data_path, index_name)
        if state is None or state.get("pulling_from"):
            # No local state, or a restore that was interrupted. Both mean the
            # real path has work to do and must not be skipped.
            needed.append(index_name)
            continue

        synced_at = state.get("synced_at_unix")
        data_path = box_meta.get_local_part_path(config, BoxPart.DATA)
        if synced_at is None or not data_path.is_dir():
            needed.append(index_name)
            continue
        _conf_exclude = (
            box_meta.get_local_part_path(config, BoxPart.CONF)
            / const.RCLONE_EXCLUDE_FILENAME
        )
        _exclude_names = literal_exclude_names(
            _conf_exclude
            if _conf_exclude.exists()
            else config.default_rclone_exclude_path
        )
        if tree_touched_since(data_path, float(synced_at), _exclude_names):
            needed.append(index_name)
            continue

        skippable.append(index_name)

    return needed, skippable

# %% [markdown]
# ## The full-pass skip: every part provable from bulk listings
#
# `_dev/FULL-PASS-SKIP-DESIGN-NOTE.md` (v5) is the design; this is its code. A
# box may be put aside for a pass only when EVERY part in the closure of what
# the pass would execute is provably unchanged, and "provable" means:
#
# - the REMOTE record is the one this machine last agreed with: its md5 from a
#   bulk `lsjson --hash` over `sync_records/` equals the md5 in the machine-local
#   sidecar `_remote_identity` wrote when the real path last read that record
#   and answered SYNCED/EXCLUDED (or completed a push or pull); and
# - for a part this machine HOLDS, the local record is complete, names the same
#   ULID the sidecar does, and the local tree matches the fingerprint baseline
#   bound to it (`local_tree_differs(...) is False` -- None, UNKNOWN, is never
#   proof).
#
# Nothing is written to the remote and nothing is stamped from a listing: both
# sides of the comparison are facts about bytes -- what the remote holds now,
# what this machine read when it agreed -- so there is no observation window
# to race, no marker to keep consistent with a record, and no bootstrap.

# %%
#|export
from boxyard._remote_identity import read_remote_identity

# (storage_location, index_name) -- the directory names as LISTED. A box's
# remote is identified by its store as well, so two stores holding a
# same-named box can never lend each other a proof. The box this machine
# knows is matched to a listed directory by BOX ID (`remote_view_for`),
# because `sync_box` resolves the remote by id: a box renamed on another
# machine lives under a name this machine does not know yet, and must be
# judged under the name the remote actually has -- and its records must sit
# under THAT name too, or the real path raises (a rename whose record move
# failed). Both found by the implementation review.
BoxKey = tuple[str, str]


def project_record_listing(
    storage_location: str, entries: list[dict[str, Any]] | None
) -> dict[BoxKey, dict[str, str | None]]:
    """
    {(store, index_name): {part: md5}} from ONE `rclone lsjson --hash` over
    `<store>/sync_records/`, depth 2, files only. An md5 of None means the
    file is there but the backend could not hash it -- identity UNKNOWN.

    Exact keying: a path must have exactly two components and the filename
    must be `<part>.rec` with one dot. rclone's `<name>.<hash>.partial` upload
    residue, the local-only sidecars and anything deeper all fall through.
    """
    view: dict[BoxKey, dict[str, str | None]] = {}
    for entry in entries or []:
        parts = Path(entry["Path"]).parts
        if len(parts) != 2:
            continue
        box, name = parts
        if not name.endswith(".rec") or name.count(".") != 1:
            continue
        md5 = (entry.get("Hashes") or {}).get("md5")
        view.setdefault((storage_location, box), {})[name[: -len(".rec")]] = (
            md5 if isinstance(md5, str) and md5 else None
        )
    return view


@dataclass
class RemoteBoxView:
    """What the `boxes/` listing says about one box: its boxmeta is present,
    a `conf/` DIRECTORY exists (however deep its contents), and the restic
    pointer's `(ModTime, Size)` if it has one."""

    index_name: str = ""
    boxmeta: bool = False
    conf_dir: bool = False
    pointer: "tuple[str | None, int | None] | None" = None


def project_box_listing(
    storage_location: str, entries: list[dict[str, Any]] | None
) -> dict[BoxKey, RemoteBoxView]:
    """
    From ONE `rclone lsjson --recursive --max-depth 2` over `<store>/boxes/`
    that lists files AND directories under filters
    `+ /*/boxmeta.toml`, `+ /*/data.snapshot`, `+ /*/conf/`, `- **`.

    The directory entry is the point: a files-only listing at any depth
    cannot prove a directory ABSENT (reproduced by the implementation review
    with `conf/nested/settings.txt` at depth 4), whereas `<box>/conf` appears
    as a directory entry in its parent's listing whether it holds one file,
    a nested tree, or nothing at all.
    """
    view: dict[BoxKey, RemoteBoxView] = {}
    for entry in entries or []:
        parts = Path(entry["Path"]).parts
        if len(parts) != 2:
            continue
        box, name = parts
        v = view.setdefault((storage_location, box), RemoteBoxView(index_name=box))
        if entry.get("IsDir"):
            if name == const.BOX_CONF_REL_PATH:
                v.conf_dir = True
        elif name == const.BOX_METAFILE_REL_PATH:
            v.boxmeta = True
        elif name == const.BOX_SNAPSHOT_POINTER_REL_PATH:
            v.pointer = (entry.get("ModTime"), entry.get("Size"))
    return view


def remote_view_for(
    boxes: dict[BoxKey, RemoteBoxView], storage_location: str, box_id: str
) -> "RemoteBoxView | None":
    """
    The listed box directory holding `box_id`'s boxmeta on `storage_location`,
    resolved by id as `sync_box` resolves it -- or None when there is not
    exactly one (absent: deleted, mid-rename, uncovered; two: a copy, which
    the real path must sort out).
    """
    matches = [
        v
        for (sl, index_name), v in boxes.items()
        if sl == storage_location and v.boxmeta and _index_has_id(index_name, box_id)
    ]
    return matches[0] if len(matches) == 1 else None


def _index_has_id(index_name: str, box_id: str) -> bool:
    try:
        return BoxMeta.parse_index_name(index_name)[0] == box_id
    except ValueError:
        return False


def closure_of(parts: "list[BoxPart]") -> "set[BoxPart]":
    """
    The parts `sync_box` actually executes for a request. DATA drags META
    (ownership is read from it) and CONF (its filters decide what DATA syncs)
    along, so a `-c data` pass must prove all three before skipping a box --
    a remote CONF edit would otherwise never reach this machine.
    """
    closure: set[BoxPart] = set(parts)
    if BoxPart.DATA in closure:
        closure |= {BoxPart.META, BoxPart.CONF}
    return closure


def _held_part_unchanged(
    config: boxyard.config.Config,
    box_meta: BoxMeta,
    part: BoxPart,
    remote_md5: str | None,
    exclude_file,
) -> bool:
    """A part this machine holds a record for. See the module note."""
    from boxyard._fingerprint import filter_signature, local_tree_differs
    from boxyard._models import SyncRecord

    if remote_md5 is None:
        return False
    rec_path = box_meta.get_local_sync_record_path(config, part)
    try:
        rec = SyncRecord.model_validate_json(rec_path.read_text())
    except (OSError, ValueError):
        return False
    if not rec.sync_complete:
        return False
    ident = read_remote_identity(rec_path)
    if ident is None or not ident["sync_complete"]:
        return False
    if ident["md5"] != remote_md5 or ident["ulid"] != str(rec.ulid):
        return False
    differs = local_tree_differs(
        local_path=box_meta.get_local_part_path(config, part),
        local_sync_record_path=rec_path,
        local_sync_record_ulid=rec.ulid,
        rclone_config_path=config.rclone_config_path,
        exclude_file=exclude_file,
        filter_sig=filter_signature(exclude_file),
    )
    return differs is False


def _unheld_part_unchanged(
    config: boxyard.config.Config,
    box_meta: BoxMeta,
    part: BoxPart,
    records: "dict[str, str | None] | None",
) -> bool:
    """
    A part this machine holds NO copy of (an excluded DATA, a CONF that was
    recorded by its creator but never materialized). Provable when the remote
    has no record at all, or when its record is the COMPLETE one this machine
    last read on the real path -- which answered EXCLUDED / SYNCED for it. An
    incomplete remote record (a push interrupted elsewhere) never gets a
    sidecar, because the real path answers INCOMPLETE and raises before it
    reaches those verdicts; so a sidecar match is proof of completeness too.
    """
    if records is None or part.value not in records:
        return True
    remote_md5 = records[part.value]
    if remote_md5 is None:
        return False
    ident = read_remote_identity(box_meta.get_local_sync_record_path(config, part))
    return ident is not None and ident["sync_complete"] and ident["md5"] == remote_md5


def meta_provably_unchanged(
    config: boxyard.config.Config,
    box_meta: BoxMeta,
    records: "dict[str, str | None] | None",
) -> bool:
    if records is None or "meta" not in records:
        return False
    return _held_part_unchanged(config, box_meta, BoxPart.META, records["meta"], None)


def conf_provably_unchanged(
    config: boxyard.config.Config,
    box_meta: BoxMeta,
    records: "dict[str, str | None] | None",
    remote_conf_dir_present: bool,
) -> bool:
    """
    Local record and local directory both absent: provable only when the
    remote has no `conf/` directory either (`new_box` pushes an EMPTY conf/,
    which rclone records but never creates on the remote -- every box looks
    like this from every machine but its creator) and the remote record, if
    any, is the complete one this machine agreed with. A remote conf tree is
    something to pull; a remote conf tree without a record is a loud ERROR on
    the real path. Any other asymmetry (a local directory nothing has synced,
    a record without its directory) goes to the real path, which pushes,
    warns or errors about it.
    """
    rec_path = box_meta.get_local_sync_record_path(config, BoxPart.CONF)
    conf_dir = box_meta.get_local_part_path(config, BoxPart.CONF)
    local_record, local_dir = rec_path.exists(), conf_dir.exists()
    if not local_record and not local_dir:
        if remote_conf_dir_present:
            return False
        return _unheld_part_unchanged(config, box_meta, BoxPart.CONF, records)
    if not (local_record and local_dir):
        return False
    if records is None or "conf" not in records:
        return False
    return _held_part_unchanged(config, box_meta, BoxPart.CONF, records["conf"], None)


def data_provably_unchanged(
    config: boxyard.config.Config,
    box_meta: BoxMeta,
    records: "dict[str, str | None] | None",
    pointer: "tuple[str | None, int | None] | None",
) -> bool:
    """
    Placement decides first, by EXACT state and for BOTH storage formats:
    `check_included()` is false for MISSING and UNAVAILABLE too, which would
    have skipped an unplugged removable root where the real path raises, and
    the restic filter on its own knows nothing about placement (the
    implementation review reproduced a RELOCATING restic box being skipped).
    EXCLUDED with nothing on disk is provable when the remote record, if any,
    is complete and agreed (plain) or trivially (restic keeps no record and
    the real path answers EXCLUDED without touching the repository); EXCLUDED
    with a tree at the path is the real path's to report; every other state
    keeps raising loudly through the real path.
    """
    from boxyard._checkout import LocalCheckoutState
    from boxyard._enums import StorageFormat

    state = box_meta.get_checkout_status(config).state
    if state is LocalCheckoutState.EXCLUDED:
        if box_meta.get_local_part_path(config, BoxPart.DATA).exists():
            return False
        if box_meta.storage_format is StorageFormat.RESTIC:
            return True
        return _unheld_part_unchanged(config, box_meta, BoxPart.DATA, records)
    if state is not LocalCheckoutState.INCLUDED:
        return False
    if box_meta.storage_format is StorageFormat.RESTIC:
        _, provable = data_boxes_needing_sync(
            config, [box_meta], {box_meta.index_name: pointer} if pointer else {}
        )
        return box_meta.index_name in provable
    if records is None or "data" not in records:
        return False
    return _held_part_unchanged(
        config,
        box_meta,
        BoxPart.DATA,
        records["data"],
        box_meta.get_effective_exclude_path(config),
    )


@dataclass
class SkipVerdicts:
    needed: list[str] = field(default_factory=list)
    skippable: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    """index_name -> why it is needed (the first gate that failed)."""


def boxes_needing_sync_full(
    config: boxyard.config.Config,
    box_metas: list[BoxMeta],
    *,
    requested_parts: "list[BoxPart]",
    records: dict[BoxKey, dict[str, str | None]],
    boxes: dict[BoxKey, RemoteBoxView],
    tombstoned: set[str],
    skip_meta: bool,
    skip_data: bool,
) -> SkipVerdicts:
    """
    Split boxes into (needed, provably unchanged) for a pass that will execute
    `closure_of(requested_parts)`.

    `records` comes from the hashed `sync_records/` listing and `boxes` from
    the `boxes/` listing, both keyed by (storage location, listed directory
    name); a box is matched to them by id, then by the name that match has. Each
    part is gated on its own flag -- `--skip-unchanged` for the whole box,
    `--skip-unchanged-meta` for META alone -- and a part without its flag is
    never provable.

    Box-level gates come first: a tombstoned box must reach `sync_box` (whose
    warning is the only report of the deletion); a box on a LOCAL storage
    location has no listing; and a box id the listing shows no `boxmeta.toml`
    for was deleted, is mid-rename, or was not covered -- the real path
    raises or warns. A box merely RENAMED on the remote is judged under its
    remote name, as `sync_box` syncs it.

    One box's failure to be judged must not abort the pass for 632 others:
    any exception while evaluating a box routes THAT box to needed, printed
    to stderr with its name. The real path then reports its own error.
    """
    import sys

    from boxyard.config import StorageType

    verdicts = SkipVerdicts()
    closure = closure_of(requested_parts)

    for box_meta in box_metas:
        index_name = box_meta.index_name
        try:
            failed: str | None = None
            if index_name in tombstoned:
                failed = "tombstoned"
            elif config.storage_locations[box_meta.storage_location].storage_type == StorageType.LOCAL:
                failed = "local-storage"
            else:
                view = remote_view_for(boxes, box_meta.storage_location, box_meta.box_id)
                if view is None:
                    failed = "remote-box-absent"
            if failed is None:
                recs = records.get((box_meta.storage_location, view.index_name))
                for part in (BoxPart.META, BoxPart.CONF, BoxPart.DATA):
                    if part not in closure:
                        continue
                    if part is BoxPart.META:
                        ok = skip_meta and meta_provably_unchanged(config, box_meta, recs)
                    elif part is BoxPart.CONF:
                        ok = skip_data and conf_provably_unchanged(
                            config, box_meta, recs, view.conf_dir
                        )
                    else:
                        ok = skip_data and data_provably_unchanged(
                            config, box_meta, recs, view.pointer
                        )
                    if not ok:
                        failed = part.value
                        break
            if failed is None:
                verdicts.skippable.append(index_name)
            else:
                verdicts.reasons[index_name] = failed
                verdicts.needed.append(index_name)
        except Exception as e:  # one box must not take the pass down
            print(
                f"--skip-unchanged: could not judge '{index_name}', syncing it "
                f"instead ({type(e).__name__}: {e})",
                file=sys.stderr,
            )
            verdicts.reasons[index_name] = f"error: {e}"
            verdicts.needed.append(index_name)

    return verdicts

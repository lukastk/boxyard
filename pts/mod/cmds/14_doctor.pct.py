# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _doctor
#
# `boxyard doctor` — a strictly read-only health check of every part of a machine's
# boxyard state, so misuse and drift get caught mechanically.
#
# Checks:
#
# 1. **unregistered-folder** — every directory inside `user_boxes_path` must be a
#    registered box (`local_store/<storage_location>/<index_name>/boxmeta.toml` exists).
# 2. **malformed-name** — entries in `user_boxes_path` whose names don't parse as a
#    valid index name `<timestamp>_<subid>__<name>`. Validity is derived from config
#    but legacy formats that exist in the wild are accepted.
# 3. **broken-registration** — `local_store/<storage>/<index>/` dirs missing
#    `boxmeta.toml`, or with a `boxmeta.toml` that fails to parse/validate.
# 4. **duplicate-box-id** — the same box id registered more than once.
# 5. **stale-cache** — `boxyard_meta.json` disagrees with a fresh scan of `local_store`.
# 6. **dangling-symlinks** — entries under `user_box_groups_path` whose targets don't exist.
# 7. **group-tree-debris** — real (non-symlink) files under `user_box_groups_path`,
#    which make `create_user_symlinks` raise and thereby break most mutating commands.
# 8. **orphaned-sync-records** — `sync_records/<index>/` with no matching local registration.
# 9. **interrupted-sync** — local sync records with `sync_complete: false` (an
#    interrupted sync; the local copy may be incomplete) or that fail to parse.
# 10. **unknown-storage-location** — `local_store/` dirs and remote-index caches that
#     match no configured storage location (left over from a removed/renamed one).
# 11. **rclone-config** — the rclone binary must be resolvable, every rclone-type
#     storage location needs a matching remote in `boxyard_rclone.conf`, and the
#     default exclude file must exist.
# 12. **stale-meta-mirror** — per rclone storage location, remote boxmetas not present
#     locally (what `sync-missing-meta` would fetch). Skippable so doctor works offline.
# 13. **tombstoned-box** — locally registered boxes whose id is tombstoned on the
#     remote (deleted from another machine). Skippable, like stale-meta-mirror.
# 14. **diverged-box** — a box that is wedged: either its local and remote sync
#     records disagree and the local copy has also changed (both sides moved on
#     independently), or a push from another machine never completed. Until this
#     check existed doctor could see neither: two boxes on macbook sat wedged
#     from March to August 2026 while doctor reported "all checks passed" on that
#     machine throughout. They were found by reading a supervisor log. A box that
#     merely needs pulling is deliberately NOT reported — see the check.
# 15. **tree-orphans** — boxmeta parents referencing unknown box ids.
# 16. **unknown-boxmeta-keys** — a `boxmeta.toml` carries a key this version of
#     boxyard does not know, i.e. it was written by a newer one. The key is
#     preserved untouched (see `BoxMeta.unknown_keys`); this check is what makes
#     that preservation visible instead of silent.
# 17. **machine-name-unset** — no `machine_name` in the config. Box write
#     ownership identifies a machine by that name and never by its hostname, so
#     until it is set this machine can never own a box.
# 18. **unknown-config-keys** — `config.toml` carries a key this version of
#     boxyard does not know, at the top level or inside one of its tables. Like
#     `unknown-boxmeta-keys`, the key is tolerated rather than fatal; this check
#     is what keeps that tolerance from turning a loud typo into a silent one.
# 19. **write-denied** — a box owned by another machine that has local changes
#     here which will never be pushed. This is the ONLY report of that state:
#     sync stays deliberately silent about it (see `SyncCondition.WRITE_DENIED`),
#     so if doctor did not say it, nothing would.
# 20. **stale-owner** — a box whose `write_owner` cannot be a working owner:
#     either it names a machine that owns nothing else in this yard, or it names
#     THIS machine for a box this machine does not have. Both mean no machine
#     can push the box. `claim` and `exclude` each close one known route into
#     that state; this check is what catches the routes nobody thought of.
# 21. **unpushed-meta-edit** — a `boxmeta.toml` that differs from the copy this
#     machine last agreed with the remote about, and no push since. Purely
#     local, and the POINT IS THE TIMING: on its own this is an ordinary pending
#     edit, but if another machine pushes that box's META first it becomes a
#     two-sided divergence that sync refuses and no automatic path resolves.
#     Forty-four boxes on macbook went that way in one afternoon in August 2026.
#     Needs the merge base (`BoxMeta.get_local_meta_base_path`), so it says
#     nothing about a box that has not synced its META since v0.5.14.
# 22. **unowned-box** — a box INCLUDED here that no machine has claimed. Unowned
#     means unrestricted, so nothing is being withheld, but multiple machines
#     can still diverge. Scoped to boxes held here because `claim` requires that.
# 23. **sync-policy-conflict** — contradictory or invalid effective sync policy.
# 24. **unusable-box-sync-conf** — unreadable per-box sync configuration.
# 25. **checkout-root-config** — duplicate or overlapping configured roots.
# 26. **checkout-root-unavailable** — guarded roots whose exact mount/UUID cannot
#     be verified, including boxes recorded there.
# 27. **checkout-placement** — unknown/malformed/orphan placements, missing
#     included DATA, or excluded boxes with DATA present.
# 28. **duplicate-checkout** — one box physically present in multiple roots.
# 29. **interrupted-relocation** — a durable local move transaction requiring
#     idempotent recovery.
#
# Doctor never mutates or auto-fixes anything.

# %%
#|default_exp cmds._doctor
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import re
from datetime import datetime
from pathlib import Path

from boxyard.config import get_config, StorageType
from boxyard import const

# %% [markdown]
# ## Check names

# %%
#|top_export
DOCTOR_CHECK_NAMES = [
    "unregistered-folder",
    "malformed-name",
    "broken-registration",
    "duplicate-box-id",
    "stale-cache",
    "dangling-symlinks",
    "group-tree-debris",
    "orphaned-sync-records",
    "interrupted-sync",
    "unknown-storage-location",
    "rclone-config",
    "stale-meta-mirror",
    "tombstoned-box",
    "diverged-box",
    "tree-orphans",
    "unknown-boxmeta-keys",
    "machine-name-unset",
    "unknown-config-keys",
    "write-denied",
    "stale-owner",
    "unpushed-meta-edit",
    "unowned-box",
    "sync-policy-conflict",
    "unusable-box-sync-conf",
    "checkout-root-config",
    "checkout-root-unavailable",
    "checkout-placement",
    "duplicate-checkout",
    "interrupted-relocation",
]

# %% [markdown]
# ## `is_valid_index_name`
#
# An index name is `<box_id>__<name>` where `<box_id>` is `<timestamp>_<subid>`.
# Validity is derived from the config (`box_timestamp_format`,
# `box_subid_character_set`, `box_subid_length`), but formats that exist in the
# wild from earlier boxyard eras are also accepted:
#
# - `20250427_000000_VOGj7__name` — date+time timestamp with a 5-char mixed-case subid
# - `20251210_bnesz__my-servers` — date-only timestamp with a 5-char subid
#
# Both timestamp formats are accepted regardless of the configured one, since a
# yard commonly contains boxes created under older configs.

# %%
#|top_export
# Subid format used by boxes created before the current config conventions.
LEGACY_BOX_SUBID_REGEX = r"[A-Za-z0-9]{5}"


def is_valid_index_name(
    index_name: str,
    subid_character_set: str,
    subid_length: int,
) -> bool:
    """
    Return True if `index_name` parses as a valid `<timestamp>_<subid>__<name>`.

    Accepts both the date-only and date+time timestamp formats, and accepts
    legacy 5-char mixed-case subids in addition to the configured
    charset/length.
    """
    if "__" not in index_name:
        return False
    box_id, name = index_name.split("__", 1)
    if not name:
        return False

    parts = box_id.split("_")
    if len(parts) == 2:
        timestamp_str, subid = parts
        timestamp_regex = r"\d{8}"
        timestamp_format = const.BOX_TIMESTAMP_FORMAT_DATE_ONLY
    elif len(parts) == 3:
        timestamp_str, subid = f"{parts[0]}_{parts[1]}", parts[2]
        timestamp_regex = r"\d{8}_\d{6}"
        timestamp_format = const.BOX_TIMESTAMP_FORMAT
    else:
        return False

    if not re.fullmatch(timestamp_regex, timestamp_str):
        return False
    try:
        datetime.strptime(timestamp_str, timestamp_format)
    except ValueError:
        return False

    subid_matches_config = len(subid) == subid_length and all(
        c in subid_character_set for c in subid
    )
    subid_matches_legacy = re.fullmatch(LEGACY_BOX_SUBID_REGEX, subid) is not None
    return subid_matches_config or subid_matches_legacy

# %%
import string

_charset = string.ascii_lowercase + string.digits

# Current format: date-only timestamp + 6-char subid from the configured charset
assert is_valid_index_name("20260620_tbmxs5__pi-rpc-set-model", _charset, 6)
# Names may contain spaces and even '__'
assert is_valid_index_name("20240805_000000_E4Dzy__Notion integration test", _charset, 6)
assert is_valid_index_name("20260623_9qkepq__sesh-ui__ui-links", _charset, 6)
# Legacy: date+time timestamp + 5-char mixed-case subid
assert is_valid_index_name("20250427_000000_VOGj7__some-name", _charset, 6)
# Legacy: date-only timestamp + 5-char subid
assert is_valid_index_name("20251210_bnesz__my-servers", _charset, 6)

# No '__' separator at all
assert not is_valid_index_name("box-with-no-id", _charset, 6)
assert not is_valid_index_name("20260227_mysystem-obsidian", _charset, 6)
assert not is_valid_index_name("sesh-iso-test", _charset, 6)
# Empty name
assert not is_valid_index_name("20260620_tbmxs5__", _charset, 6)
# Invalid date
assert not is_valid_index_name("20261399_tbmxs5__box", _charset, 6)
# Subid neither matches config nor legacy
assert not is_valid_index_name("20260620_tbmx__box", _charset, 6)
assert not is_valid_index_name("20260620_TBMXS5__box", _charset, 6)

# %% [markdown]
# ## Function signature

# %%
#|set_func_signature
async def run_doctor(
    config_path: Path,
    check_remote: bool = True,
    storage_locations: list[str] | None = None,
) -> dict:
    """
    Run a strictly read-only health check of the machine's boxyard state.

    Args:
        config_path: Path to the boxyard config file.
        check_remote: If False, skip checks that access remote storage
            (stale-meta-mirror and tombstoned-box), so doctor works offline.
        storage_locations: If given, restrict the remote checks to these
            storage locations. Local checks always cover all storage
            locations.

    Returns:
        A JSON-serializable report:
        {
            "healthy": bool,
            "num_findings": int,
            "checks": {
                "<check-name>": {"skipped": bool, "findings": [
                    {"message": str, "hint": str, ...}
                ]},
                ...
            },
        }
    """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_boxyards

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %%
# Args
config_path = config_path
check_remote = True
storage_locations = None

# %%
# Set up a couple of synced boxes
from boxyard.cmds import new_box, sync_box

box_index_name1 = new_box(
    config_path=config_path, box_name="doctor-test-box1", storage_location="my_remote"
)
box_index_name2 = new_box(
    config_path=config_path, box_name="doctor-test-box2", storage_location="my_remote"
)
await sync_box(config_path=config_path, box_index_name=box_index_name1)
await sync_box(config_path=config_path, box_index_name=box_index_name2)

# %%
# Manufacture drift for doctor to find:
_test_config = get_config(config_path)

# An unregistered, malformed-name folder in user_boxes_path (hand-created, not via `boxyard new`)
(_test_config.user_boxes_path / "hand-made-folder").mkdir(parents=True)

# A dangling symlink in user_box_groups_path
_dangling = _test_config.user_box_groups_path / "all" / "ghost-box"
_dangling.parent.mkdir(parents=True, exist_ok=True)
_dangling.symlink_to(_test_config.user_boxes_path / "does-not-exist")

# An orphaned sync-record directory
(_test_config.boxyard_data_path / const.SYNC_RECORDS_REL_PATH / "20990101_zzzzzz__ghost-box").mkdir(
    parents=True
)

# A real (non-symlink) file in the group tree
(_test_config.user_box_groups_path / "all" / "debris.txt").write_text("debris")

# An interrupted sync: rewrite a synced box's data sync record as incomplete
from boxyard._models import SyncRecord as _SyncRecord

_incomplete_rec_path = (
    _test_config.boxyard_data_path / const.SYNC_RECORDS_REL_PATH / box_index_name1 / "data.rec"
)
_incomplete_rec_path.write_text(
    _SyncRecord.create(sync_complete=False, syncer_hostname="test-host").model_dump_json()
)

# A local_store dir for a storage location that is not in the config
(_test_config.local_store_path / "ghost-storage").mkdir(parents=True)

# A remote box that is not mirrored locally (as if created by another machine)
import tomli_w as _tomli_w

_foreign_index_name = "20990101_aaaaaa__foreign-box"
_foreign_box_path = remote_rclone_path / "boxyard" / const.REMOTE_BOXES_REL_PATH / _foreign_index_name
_foreign_box_path.mkdir(parents=True)
(_foreign_box_path / const.BOX_METAFILE_REL_PATH).write_text(
    _tomli_w.dumps({"storage_location": "my_remote", "creator_hostname": "other-machine", "groups": [], "parents": []})
)

# A tombstone on the remote for a box that is still registered locally
# (as if the box was deleted from another machine)
import json as _json
from datetime import datetime as _datetime, timezone as _timezone

_tombstoned_box_id = box_index_name2.split("__", 1)[0]
_tombstones_path = remote_rclone_path / "boxyard" / "tombstones"
_tombstones_path.mkdir(parents=True)
(_tombstones_path / f"{_tombstoned_box_id}.json").write_text(
    _json.dumps(
        {
            "box_id": _tombstoned_box_id,
            "deleted_at_utc": _datetime.now(_timezone.utc).isoformat(),
            "deleted_by_hostname": "other-machine",
            "last_known_name": "doctor-test-box2",
        }
    )
)

# %% [markdown]
# # Function body

# %% [markdown]
# Process args and set up the report structure. All checks are present in the
# report; checks that don't run are marked `skipped`.

# %%
#|export
config = get_config(config_path)

if storage_locations is not None:
    _unknown_sls = [sl for sl in storage_locations if sl not in config.storage_locations]
    if _unknown_sls:
        raise ValueError(f"Invalid storage location(s): {_unknown_sls}")

checks: dict[str, dict] = {
    check_name: {"skipped": False, "findings": []} for check_name in DOCTOR_CHECK_NAMES
}


def _add_finding(check_name: str, message: str, hint: str, **extra) -> None:
    finding = {"message": message, "hint": hint}
    for key, value in extra.items():
        finding[key] = str(value) if isinstance(value, Path) else value
    checks[check_name]["findings"].append(finding)

# %% [markdown]
# Scan `local_store` tolerantly. This is the foundation for several checks:
# it collects every registration directory, every loadable `BoxMeta`, and a
# `broken-registration` finding for everything in between. (We deliberately do
# not use `create_boxyard_meta` here — it raises on the first broken
# registration, and `get_boxyard_meta` writes the cache file when missing,
# which would violate doctor's read-only guarantee.)

# %%
#|export
from boxyard._models import BoxMeta, BoxyardMeta

registration_dirs_by_sl: dict[str, set[str]] = {}  # dirs in local_store/<sl>/
registered_index_names: set[str] = set()  # dirs that have a boxmeta.toml
problem_index_names: set[str] = set()  # dirs whose boxmeta is missing or fails to load
box_metas: list[BoxMeta] = []

for sl_name in config.storage_locations:
    registration_dirs_by_sl[sl_name] = set()
    sl_local_path = config.local_store_path / sl_name
    if not sl_local_path.is_dir():
        continue
    for entry in sorted(sl_local_path.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if not entry.is_dir():
            _add_finding(
                "broken-registration",
                f"Stray file in local store: '{entry}'",
                "Only box registration directories belong in the local store; move or delete the file.",
                path=entry,
                storage_location=sl_name,
            )
            continue
        registration_dirs_by_sl[sl_name].add(entry.name)
        boxmeta_path = entry / const.BOX_METAFILE_REL_PATH
        if not boxmeta_path.is_file():
            problem_index_names.add(entry.name)
            _add_finding(
                "broken-registration",
                f"Registration '{sl_name}/{entry.name}' has no {const.BOX_METAFILE_REL_PATH}",
                "The registration is incomplete; restore its boxmeta.toml (e.g. `boxyard sync-missing-meta`) or delete the directory if the box is gone.",
                path=entry,
                storage_location=sl_name,
            )
            continue
        try:
            box_metas.append(BoxMeta.load(config, sl_name, entry.name))
        except Exception as e:
            problem_index_names.add(entry.name)
            _add_finding(
                "broken-registration",
                f"Registration '{sl_name}/{entry.name}' has a boxmeta.toml that fails to load: {e}",
                "Fix or restore the boxmeta.toml; every mutating boxyard command will fail while it is invalid.",
                path=boxmeta_path,
                storage_location=sl_name,
            )
        else:
            registered_index_names.add(entry.name)

# %% [markdown]
# ## Checks: checkout roots and machine-local placement

# %%
#|export
from boxyard._checkout import (
    CheckoutPlacement,
    PlacementState,
    LocalCheckoutState,
    get_checkout_root_status,
    get_box_checkout_status,
    placement_path,
    root_configuration_findings,
 )

_root_statuses = {}
for _root_name in sorted(config.configured_checkout_roots):
    _root_status = get_checkout_root_status(config, _root_name)
    _root_statuses[_root_name] = _root_status
    if not _root_status.available:
        _add_finding(
            "checkout-root-unavailable",
            f"Checkout root '{_root_name}' at '{_root_status.path}' is unavailable: {_root_status.reason}",
            "Restore the configured mount/device identity. Boxyard will not create, read, or mutate checkouts beneath this root until it matches.",
            checkout_root=_root_name,
            path=_root_status.path,
        )

for _root_problem in root_configuration_findings(config):
    _add_finding(
        "checkout-root-config",
        f"Unsafe {_root_problem['kind']} checkout roots {', '.join(_root_problem['names'])} at '{_root_problem['path']}'",
        "Give every checkout root a unique, non-overlapping path before running mutations.",
        checkout_roots=_root_problem["names"],
        path=_root_problem["path"],
    )

_known_box_ids = {bm.box_id for bm in box_metas}
if config.placements_path.is_dir():
    for _placement_file in sorted(config.placements_path.glob("*.json")):
        if _placement_file.stem not in _known_box_ids:
            _add_finding(
                "checkout-placement",
                f"Placement record '{_placement_file}' has no registered box id",
                "Delete it only after confirming the box registration is permanently gone.",
                path=_placement_file,
            )

_checkout_by_id = {}
_placement_problem_ids = set()
for _bm in box_metas:
    _placement_file = placement_path(config, _bm.box_id)
    _copies = []
    _seen_copy_paths = set()
    for _name, _status in _root_statuses.items():
        if not _status.available:
            continue
        _candidate = _status.path / _bm.index_name
        _resolved_candidate = _candidate.resolve(strict=False)
        if _candidate.is_dir() and _resolved_candidate not in _seen_copy_paths:
            _seen_copy_paths.add(_resolved_candidate)
            _copies.append((_name, _candidate))
    if len(_copies) > 1:
        _add_finding(
            "duplicate-checkout",
            f"Box '{_bm.index_name}' has physical copies in multiple checkout roots: "
            + ", ".join(f"{name}={path}" for name, path in _copies),
            "Compare the copies, then keep only the one named by the placement record and move the others outside all checkout roots.",
            index_name=_bm.index_name,
            checkout_roots=[name for name, _ in _copies],
        )
    try:
        _checkout = get_box_checkout_status(config, _bm)
        _checkout_by_id[_bm.box_id] = _checkout
    except Exception as e:
        _add_finding(
            "checkout-placement",
            f"Placement for '{_bm.index_name}' cannot be loaded: {e}",
            "Repair the machine-local JSON placement record; do not put checkout_root in synced boxmeta.toml.",
            index_name=_bm.index_name,
            path=_placement_file,
        )
        _placement_problem_ids.add(_bm.box_id)
        continue

    if _checkout.root_status is None:
        _add_finding(
            "checkout-placement",
            f"Placement for '{_bm.index_name}' refers to unknown checkout root '{_checkout.checkout_root}'",
            f"Re-add that root to config, or repair '{_placement_file}' to name the correct configured root.",
            index_name=_bm.index_name,
            checkout_root=_checkout.checkout_root,
            path=_placement_file,
        )
        _placement_problem_ids.add(_bm.box_id)
        continue

    if _checkout.state == LocalCheckoutState.UNAVAILABLE:
        _add_finding(
            "checkout-root-unavailable",
            f"Box '{_bm.index_name}' is included in unavailable checkout root '{_checkout.checkout_root}'",
            "Restore the configured mount. The box remains included in the catalog and will not fall back to the default root.",
            index_name=_bm.index_name,
            checkout_root=_checkout.checkout_root,
            local_path=_checkout.local_path,
        )
        continue  # never inspect paths beneath an unavailable guarded root

    if _checkout.state == LocalCheckoutState.EXCLUDED and _copies:
        _add_finding(
            "checkout-placement",
            f"Box '{_bm.index_name}' is recorded excluded but DATA exists at '{_copies[0][1]}'",
            "Move the untracked copy aside, or repair placement only after confirming it is the intended complete checkout.",
            index_name=_bm.index_name,
            local_path=_copies[0][1],
        )

    if _checkout.state == LocalCheckoutState.MISSING:
        _add_finding(
            "checkout-placement",
            f"Included box '{_bm.index_name}' is missing from recorded root '{_checkout.checkout_root}' at '{_checkout.local_path}'",
            f"Restore it with `boxyard include -r '{_bm.index_name}'`, or inspect the placement record before changing state.",
            index_name=_bm.index_name,
            checkout_root=_checkout.checkout_root,
            local_path=_checkout.local_path,
        )
    if _checkout.placement.state == PlacementState.RELOCATING:
        _record = _checkout.placement.relocation
        _add_finding(
            "interrupted-relocation",
            f"Box '{_bm.index_name}' has an interrupted relocation from '{_record.source_root}' to '{_record.destination_root}' in phase '{_record.phase.value}'",
            f"Recover it idempotently with `boxyard relocate -r '{_bm.index_name}'` (the destination is recorded).",
            index_name=_bm.index_name,
            source_root=_record.source_root,
            destination_root=_record.destination_root,
            phase=_record.phase.value,
        )

# %% [markdown]
# ## Checks: `unregistered-folder` and `malformed-name` across checkout roots

# %%
#|export
all_registration_dirs = set().union(*registration_dirs_by_sl.values())

for _root_name, _root_status in _root_statuses.items():
    if not _root_status.available or not _root_status.path.is_dir():
        continue
    for entry in sorted(_root_status.path.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if not entry.is_dir():
            _add_finding(
                "unregistered-folder",
                f"Stray file in checkout root '{_root_name}': '{entry}'",
                f"Only box directories belong in checkout root '{_root_name}'; move the file into a box or outside the root.",
                path=entry,
                checkout_root=_root_name,
            )
            continue
        if entry.name not in registered_index_names:
            _add_finding(
                "unregistered-folder",
                f"Directory '{entry.name}' in checkout root '{_root_name}' is not a registered box",
                f"Register it with `boxyard new --from <path> --checkout-root '{_root_name}' -n <name>`, or move it outside all checkout roots.",
                path=entry,
                checkout_root=_root_name,
            )
        if not is_valid_index_name(
            entry.name, config.box_subid_character_set, config.box_subid_length
        ):
            _add_finding(
                "malformed-name",
                f"Directory name '{entry.name}' in checkout root '{_root_name}' does not parse as an index name '<timestamp>_<subid>__<name>'",
                "Boxes must be created via `boxyard new`, which generates the index name; move the folder or register it with `boxyard new --from <path> --checkout-root <root>`.",
                path=entry,
                checkout_root=_root_name,
            )

# %% [markdown]
# ## Check: `duplicate-box-id`
#
# Box ids must be unique across the whole yard — `boxyard_meta.by_id` silently
# keeps only one of the duplicates, so everything keyed by id misbehaves.

# %%
#|export
_metas_by_id: dict[str, list[BoxMeta]] = {}
for bm in box_metas:
    _metas_by_id.setdefault(bm.box_id, []).append(bm)

# %% [markdown]
# ## `sync-policy-conflict` and `unusable-box-sync-conf`
#
# Resolving a box's cadence REFUSES ambiguity rather than joining it, so a box
# matching two policies that disagree needs a person. `multi-sync` reports the
# conflict and syncs the box anyway -- the ambiguity is about how often, never
# about whether -- but a message in an unattended loop's stderr is not where a
# configuration mistake should live, so doctor names it too.
#
# By design this fires on nothing today: sync policy keys off LIFECYCLE groups
# (`archived`, `dormant`), and measured across the 590-box yard no box carries
# both. It only speaks up when genuinely contradictory policies are asked for.

# %%
#|export
from boxyard._sync_policy import PolicyConflict as _PolicyConflict
from boxyard._sync_policy import resolve_policy as _resolve_policy

for _bm in sorted(box_metas, key=lambda b: b.index_name):
    try:
        _resolve_policy(config, _bm)
    except _PolicyConflict as _conflict:
        _add_finding(
            "sync-policy-conflict",
            str(_conflict),
            "Two sync policies claim this box and state different values for "
            "the same setting. Either stop the box matching both groups, or "
            "settle it on the box itself in `conf/sync.toml` -- a box-level "
            "setting beats every policy. The box still syncs meanwhile, on "
            "whichever cadence the pass resolved.",
            box_index_name=_bm.index_name,
        )
    except ValueError as _bad_conf:
        _add_finding(
            "unusable-box-sync-conf",
            f"Box '{_bm.index_name}': {_bad_conf}",
            "The box's own `conf/sync.toml` cannot be read, so the cadence it "
            "asks for is not being applied. It was written deliberately, so "
            "boxyard refuses to guess: fix the file, or delete it to fall back "
            "to the group policy.",
            box_index_name=_bm.index_name,
        )

for _box_id, _bms in sorted(_metas_by_id.items()):
    if len(_bms) > 1:
        _locations = ", ".join(f"{bm.storage_location}/{bm.index_name}" for bm in _bms)
        _add_finding(
            "duplicate-box-id",
            f"Box id '{_box_id}' is registered {len(_bms)} times: {_locations}",
            "Box ids must be unique. This usually means the box was RENAMED on "
            "another machine: `sync-missing-meta` fetched the new name while the "
            "old registration stayed behind. The remote's name is authoritative "
            "— check it with `boxyard copy`/`rclone lsf` or on the machine that "
            "owns the box, then remove the registration whose name the remote "
            "does not have. Do NOT re-create the box; that would mint a new id.",
            box_id=_box_id,
        )

# %% [markdown]
# ## Check: `stale-cache`
#
# `boxyard_meta.json` is a cache of the `local_store` scan. If it disagrees
# with a fresh scan, `boxyard list` (and everything built on it) shows stale
# data. Registrations already flagged as `broken-registration` are excluded
# from the comparison to avoid double-reporting.

# %%
#|export
if not config.boxyard_meta_path.is_file():
    _add_finding(
        "stale-cache",
        f"Cache file '{config.boxyard_meta_path}' does not exist",
        "Run `boxyard create-user-symlinks` (or any mutating boxyard command) to regenerate it.",
        path=config.boxyard_meta_path,
    )
else:
    _cached_meta = None
    try:
        _cached_meta = BoxyardMeta.model_validate_json(config.boxyard_meta_path.read_text())
    except Exception as e:
        _add_finding(
            "stale-cache",
            f"Cache file '{config.boxyard_meta_path}' fails to parse: {e}",
            "Run `boxyard create-user-symlinks` (or any mutating boxyard command) to regenerate it.",
            path=config.boxyard_meta_path,
        )
    if _cached_meta is not None:
        _stale_cache_hint = "Run `boxyard create-user-symlinks` (or any mutating boxyard command) to refresh the cache."
        _fresh_by_key = {(bm.storage_location, bm.index_name): bm for bm in box_metas}
        _cached_by_key = {
            (bm.storage_location, bm.index_name): bm for bm in _cached_meta.box_metas
        }
        for _key in sorted(set(_cached_by_key) - set(_fresh_by_key)):
            if _key[1] in problem_index_names:
                continue  # already reported as broken-registration
            _add_finding(
                "stale-cache",
                f"Cache contains '{_key[0]}/{_key[1]}' but there is no such registration in the local store",
                _stale_cache_hint,
                storage_location=_key[0],
                index_name=_key[1],
            )
        for _key in sorted(set(_fresh_by_key) - set(_cached_by_key)):
            _add_finding(
                "stale-cache",
                f"Registration '{_key[0]}/{_key[1]}' is missing from the cache",
                _stale_cache_hint,
                storage_location=_key[0],
                index_name=_key[1],
            )
        for _key in sorted(set(_fresh_by_key) & set(_cached_by_key)):
            if _fresh_by_key[_key].model_dump() != _cached_by_key[_key].model_dump():
                _add_finding(
                    "stale-cache",
                    f"Cache entry for '{_key[0]}/{_key[1]}' is out of date",
                    _stale_cache_hint,
                    storage_location=_key[0],
                    index_name=_key[1],
                )

# %% [markdown]
# ## Checks: `dangling-symlinks` and `group-tree-debris`
#
# One walk over `user_box_groups_path`: symlinks whose targets don't exist, and
# real (non-symlink) files, which make `create_user_symlinks` raise — breaking
# every command that refreshes the symlinks (most mutating commands).

# %%
#|export
def _walk_group_tree(path: Path) -> None:
    # Explicit walk that never descends into symlinks — Path.rglob follows
    # directory symlinks on Python < 3.13, which would scan inside every box.
    for entry in sorted(path.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            if not entry.exists():
                _add_finding(
                    "dangling-symlinks",
                    f"Symlink '{entry}' points to a non-existent target '{entry.readlink()}'",
                    "Run `boxyard create-user-symlinks` to rebuild the group symlinks.",
                    path=entry,
                )
        elif entry.is_dir():
            _walk_group_tree(entry)
        else:
            _add_finding(
                "group-tree-debris",
                f"'{entry}' in the user box groups path is a real file, not a symlink",
                "`boxyard create-user-symlinks` (called by most mutating commands) refuses to run while real files are in the group tree; move or delete the file.",
                path=entry,
            )


if config.user_box_groups_path.is_dir():
    _walk_group_tree(config.user_box_groups_path)

# %% [markdown]
# ## Check: `orphaned-sync-records`
#
# `sync_records/<index_name>/` directories with no matching registration in
# the local store (typically left over after a registration was removed by
# hand).

# %%
#|export
_sync_records_path = config.boxyard_data_path / const.SYNC_RECORDS_REL_PATH

if _sync_records_path.is_dir():
    for entry in sorted(_sync_records_path.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.name not in all_registration_dirs:
            _add_finding(
                "orphaned-sync-records",
                f"Sync records exist for '{entry.name}' but there is no such registration in the local store",
                "Left over from a deleted or renamed box; delete the directory if the box is really gone.",
                path=entry,
            )

# %% [markdown]
# ## Check: `interrupted-sync`
#
# Local sync records with `sync_complete: false` mean a sync was interrupted —
# an interrupted pull leaves the *local* copy potentially incomplete
# (`SYNC_FROM_REMOTE_INCOMPLETE`). Only `box-status` reveals this today, and it
# needs remote access; doctor catches it offline by reading the record files.
# Records that fail to parse are flagged too. Record dirs without a matching
# registration are skipped — those are already `orphaned-sync-records` findings.

# %%
#|export
from boxyard._models import SyncRecord

if _sync_records_path.is_dir():
    for entry in sorted(_sync_records_path.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if entry.name not in all_registration_dirs:
            continue  # already reported as orphaned-sync-records
        for rec_path in sorted(entry.glob("*.rec")):
            try:
                rec = SyncRecord.model_validate_json(rec_path.read_text())
            except Exception as e:
                _add_finding(
                    "interrupted-sync",
                    f"Sync record '{rec_path}' fails to parse: {e}",
                    f"Inspect the box with `boxyard box-status -r '{entry.name}'`; a fresh `boxyard sync` rewrites the record.",
                    path=rec_path,
                    index_name=entry.name,
                )
                continue
            if not rec.sync_complete:
                _add_finding(
                    "interrupted-sync",
                    f"A {rec_path.stem} sync of '{entry.name}' was interrupted and never completed (record from host '{rec.syncer_hostname}' at {rec.timestamp})",
                    f"The local copy may be incomplete. Inspect with `boxyard box-status -r '{entry.name}'` and re-run `boxyard sync -r '{entry.name}'` to recover.",
                    path=rec_path,
                    index_name=entry.name,
                )

# %% [markdown]
# ## Check: `unknown-storage-location`
#
# Directories under `local_store/` (and remote-index cache files) that match no
# configured storage location — left over when a storage location is removed or
# renamed in the config. Invisible to every other command, since they all
# iterate the configured storage locations.

# %%
#|export
if config.local_store_path.is_dir():
    for entry in sorted(config.local_store_path.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if not entry.is_dir():
            _add_finding(
                "unknown-storage-location",
                f"Stray file in the local store root: '{entry}'",
                "Only per-storage-location directories belong in the local store root; move or delete the file.",
                path=entry,
            )
        elif entry.name not in config.storage_locations:
            _add_finding(
                "unknown-storage-location",
                f"Local store contains '{entry.name}' but no such storage location is configured",
                "Left over from a removed or renamed storage location; delete the directory, or re-add the storage location to the config.",
                path=entry,
            )

if config.remote_indexes_path.is_dir():
    for entry in sorted(config.remote_indexes_path.iterdir(), key=lambda p: p.name):
        if entry.suffix == ".json" and entry.stem not in config.storage_locations:
            _add_finding(
                "unknown-storage-location",
                f"Remote index cache '{entry.name}' matches no configured storage location",
                "Left over from a removed or renamed storage location; delete the file.",
                path=entry,
            )

# %% [markdown]
# ## Check: `rclone-config`
#
# Environment/configuration prerequisites for syncing: the rclone binary must be
# resolvable, every rclone-type storage location needs a matching remote section
# in boxyard's own rclone config, and the default exclude file must exist (data
# syncs of boxes without their own `conf/.rclone_exclude` pass it to rclone
# unconditionally). `_rclone_available` is reused below: when the binary is
# unresolvable the remote checks are skipped instead of crashing doctor.

# %%
#|export
_rclone_available = True
try:
    from boxyard._utils.rclone import get_rclone_binary

    get_rclone_binary()
except Exception as e:
    _rclone_available = False
    _add_finding(
        "rclone-config",
        f"The rclone binary could not be resolved: {e}",
        f"Install rclone, or point boxyard at it via the {const.ENV_VAR_BOXYARD_RCLONE} env var or the `rclone_path` config key.",
    )

_rclone_sl_names = [
    sl_name
    for sl_name, sl_config in config.storage_locations.items()
    if sl_config.storage_type == StorageType.RCLONE
]

if _rclone_sl_names:
    if not config.rclone_config_path.is_file():
        _add_finding(
            "rclone-config",
            f"rclone config '{config.rclone_config_path}' does not exist, but there are rclone storage locations: {', '.join(_rclone_sl_names)}",
            "Boxyard uses its own rclone config (not ~/.config/rclone); recreate it with a remote per rclone storage location.",
            path=config.rclone_config_path,
        )
    else:
        import configparser

        _rclone_conf_parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            _rclone_conf_parser.read_string(config.rclone_config_path.read_text())
        except configparser.Error as e:
            _add_finding(
                "rclone-config",
                f"rclone config '{config.rclone_config_path}' fails to parse: {e}",
                "Fix the file; every rclone operation depends on it.",
                path=config.rclone_config_path,
            )
        else:
            for sl_name in _rclone_sl_names:
                if sl_name not in _rclone_conf_parser.sections():
                    _add_finding(
                        "rclone-config",
                        f"Storage location '{sl_name}' has no [{sl_name}] remote in '{config.rclone_config_path}'",
                        f"Add a [{sl_name}] remote to the rclone config, or remove the storage location from the boxyard config.",
                        storage_location=sl_name,
                    )

if not config.default_rclone_exclude_path.is_file():
    _add_finding(
        "rclone-config",
        f"Default exclude file '{config.default_rclone_exclude_path}' does not exist",
        "Data syncs of boxes without their own conf/.rclone_exclude will fail; re-run `boxyard init` to recreate it (existing config is preserved).",
        path=config.default_rclone_exclude_path,
    )

# %% [markdown]
# ## Checks: `stale-meta-mirror` and `tombstoned-box` (remote)
#
# Per rclone storage location:
#
# - **stale-meta-mirror** — list the remote boxmetas and report the ones not
#   present in the local meta mirror; exactly what `boxyard sync-missing-meta`
#   would fetch. A machine where this never runs silently hides newer boxes
#   from `boxyard list` and everything built on it.
# - **tombstoned-box** — locally registered boxes whose id has a tombstone on
#   the remote: the box was deleted from another machine and will only ever
#   produce `TOMBSTONED` sync errors here. Tombstone filenames are the box ids
#   (see `_tombstones.get_tombstone_path`), so one listing suffices.
#
# Both are skipped when `check_remote` is False so doctor works offline, and
# when the rclone binary is unresolvable (already a `rclone-config` finding) so
# doctor reports instead of crashing. A failure to list the remote is a loud
# finding rather than being treated as "no boxes".

# %%
#|export
if not check_remote or not _rclone_available:
    checks["stale-meta-mirror"]["skipped"] = True
    checks["tombstoned-box"]["skipped"] = True
else:
    from boxyard._utils import rclone_lsjson

    for sl_name, sl_config in config.storage_locations.items():
        if sl_config.storage_type != StorageType.RCLONE:
            continue
        if storage_locations is not None and sl_name not in storage_locations:
            continue

        _ls_remote = await rclone_lsjson(
            config.rclone_config_path,
            source=sl_name,
            source_path=sl_config.store_path / const.REMOTE_BOXES_REL_PATH,
            files_only=True,
            recursive=True,
            filter=[f"+ {const.BOX_METAFILE_REL_PATH}"],
            max_depth=2,
        )
        _remote_reachable = _ls_remote is not None
        if _ls_remote is None:
            # rclone_lsjson conflates "path does not exist" with "remote
            # unreachable". Probe the remote root (which always exists on a
            # reachable remote) to tell the two apart: a reachable remote with
            # no boxes directory yet is just an empty remote, not a finding.
            _ls_root = await rclone_lsjson(
                config.rclone_config_path,
                source=sl_name,
                source_path="",
            )
            _remote_reachable = _ls_root is not None
            if not _remote_reachable:
                _add_finding(
                    "stale-meta-mirror",
                    f"Could not list remote storage location '{sl_name}'",
                    "Check connectivity and the rclone config, or run doctor with --no-remote to skip remote checks.",
                    storage_location=sl_name,
                )
        else:
            _remote_index_names = {
                Path(f["Path"]).parts[0] for f in _ls_remote if len(Path(f["Path"]).parts) == 2
            }
            _missing_locally = sorted(_remote_index_names - registration_dirs_by_sl[sl_name])
            if _missing_locally:
                _add_finding(
                    "stale-meta-mirror",
                    f"{len(_missing_locally)} remote box(es) on '{sl_name}' are not mirrored locally (newest: {_missing_locally[-1]})",
                    f"Run `boxyard sync-missing-meta -s {sl_name}` to fetch the missing boxmetas.",
                    storage_location=sl_name,
                    missing_index_names=_missing_locally,
                )

        if _remote_reachable:
            # Tombstones live at <store_path>/tombstones/<box_id>.json. The
            # remote is known reachable here, so a failed listing just means
            # the tombstones directory doesn't exist (no box ever deleted).
            _ls_tombstones = await rclone_lsjson(
                config.rclone_config_path,
                source=sl_name,
                source_path=sl_config.store_path / "tombstones",
                files_only=True,
            )
            _tombstoned_ids = {
                Path(f["Name"]).stem
                for f in (_ls_tombstones or [])
                if f.get("Name", "").endswith(".json")
            }
            for _reg_name in sorted(registration_dirs_by_sl[sl_name]):
                if "__" not in _reg_name:
                    continue
                if _reg_name.split("__", 1)[0] in _tombstoned_ids:
                    _add_finding(
                        "tombstoned-box",
                        f"Box '{_reg_name}' on '{sl_name}' is tombstoned on the remote (deleted from another machine) but still registered locally",
                        f"Remove the local copy with `boxyard delete -r '{_reg_name}'`, or remove the remote tombstone to resurrect the box.",
                        storage_location=sl_name,
                        index_name=_reg_name,
                        box_id=_reg_name.split("__", 1)[0],
                    )

# %% [markdown]
# ## Check: `diverged-box`
#
# A box whose LOCAL and REMOTE sync records disagree has had both sides move on
# independently — the `CONFLICT` condition, which no other check could see.
# Doctor reported "all checks passed" on a machine with two boxes wedged since
# March 2026; they surfaced only in the supervisor log, once per pass, for five
# months.
#
# The remote records are fetched in ONE bulk copy rather than a round trip per
# box: the per-box form is ~4 rclone calls x 3 parts x 583 boxes, which would
# make doctor unusable. The records are a few hundred KB in total.
#
# Only boxes registered locally are examined — a box with no local copy cannot
# have diverged. Whether the local tree has been modified since its own record
# is what separates a plain `needs_pull` (harmless, the next sync fixes it) from
# a real divergence, and that uses the same exclude-aware scan the sync engine
# uses, so the two agree.

# %%
#|export
if not check_remote or not _rclone_available:
    checks["diverged-box"]["skipped"] = True
else:
    from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone
    from boxyard._models import BoxPart, SyncRecord as _SyncRecord
    from boxyard._utils import (
        check_last_time_modified,
        literal_exclude_names,
        rclone_lsjson,
        rclone_cat,
    )
    from boxyard._utils.rclone import RcloneFailed

    # A remote record written at our own record's moment IS our record, so only
    # the rest need a round trip. rclone stamps the destination with the source
    # temp file's mtime, which is the moment the ULID was minted, so for one and
    # the same record the two agree closely: measured across 750 records on this
    # fleet, the worst gap was 2.1s, and a 5s window costs zero extra fetches.
    #
    # The window is a real, if narrow, blind spot -- two DIFFERENT pushes of the
    # same box landing within 5s of each other would look like one. That is a
    # far smaller risk than the one being removed here, and the exact
    # alternative (fetching all ~2400 records) takes over two minutes and opens
    # enough SFTP connections to disturb the syncs running alongside doctor.
    _RECORD_TIME_SLACK = _timedelta(seconds=5)

    # Long enough that no real push on this fleet is still running (the largest
    # box is ~100 GB and pushes in well under this), short enough that a genuine
    # wedge is reported the same day rather than months later.
    _INCOMPLETE_REMOTE_GRACE = _timedelta(hours=6)
    _now = _datetime.now(_timezone.utc)

    def _parse_rclone_modtime(raw: str) -> "_datetime":
        # rclone emits RFC3339 with up to nanosecond precision, which
        # `fromisoformat` cannot parse on 3.11. Truncate to microseconds.
        text = raw.replace("Z", "+00:00")
        if "." in text:
            head, _, tail = text.partition(".")
            frac, sign, offset = (
                tail.partition("+") if "+" in tail else tail.partition("-")
            )
            text = f"{head}.{frac[:6]}{sign}{offset}"
        return _datetime.fromisoformat(text)

    for _sl_name, _sl_config in config.storage_locations.items():
        if _sl_config.storage_type != StorageType.RCLONE:
            continue
        if storage_locations is not None and _sl_name not in storage_locations:
            continue

        _boxes_here = [
            bm for bm in box_metas
            if bm.storage_location == _sl_name
            and bm.box_id not in _placement_problem_ids
            and _checkout_by_id[bm.box_id].state != LocalCheckoutState.UNAVAILABLE
        ]
        if not _boxes_here:
            continue

        # ONE listing for every record on the remote. The per-box form would be
        # thousands of round trips; this is a single call.
        try:
            _ls_recs = await rclone_lsjson(
                config.rclone_config_path,
                source=_sl_name,
                source_path=_sl_config.store_path / const.SYNC_RECORDS_REL_PATH,
                files_only=True,
                recursive=True,
            )
        except RcloneFailed as e:
            # An unreachable remote is a real inability to answer the question.
            # Never report "no divergence" when we could not look -- silence here
            # would be the false all-clear this check exists to end -- but do not
            # crash doctor either, so the other checks still report.
            _add_finding(
                "diverged-box",
                f"Could not list the sync records on '{_sl_name}', so no box on it "
                f"could be checked for divergence: {e}",
                "Check connectivity and the rclone config, or pass --no-remote to skip "
                "the remote checks deliberately.",
                storage_location=_sl_name,
            )
            continue
        if _ls_recs is None:
            continue  # nothing has ever been pushed here; an empty remote is fine

        _remote_modtimes = {}
        for _entry in _ls_recs:
            _parts = Path(_entry["Path"]).parts
            if len(_parts) != 2 or not _parts[1].endswith(".rec"):
                continue
            _remote_modtimes[(_parts[0], _parts[1][: -len(".rec")])] = (
                _parse_rclone_modtime(_entry["ModTime"])
            )

        for _bm in _boxes_here:
            for _part in BoxPart:
                _local_rec_path = _bm.get_local_sync_record_path(config, _part)
                if not _local_rec_path.exists():
                    continue
                _remote_modtime = _remote_modtimes.get((_bm.index_name, _part.value))
                if _remote_modtime is None:
                    continue  # nothing pushed for this part
                try:
                    _local_rec = _SyncRecord.model_validate_json(
                        _local_rec_path.read_text()
                    )
                except Exception:
                    continue  # a malformed record is `interrupted-sync`'s business

                if not _local_rec.sync_complete:
                    continue  # `interrupted-sync` already owns this one

                # The prefilter: a remote record written at our own record's
                # moment IS our record. Only the rest are worth a round trip.
                if abs(_remote_modtime - _local_rec.ulid.datetime) <= _RECORD_TIME_SLACK:
                    continue

                _exists, _raw = await rclone_cat(
                    rclone_config_path=config.rclone_config_path,
                    source=_sl_name,
                    source_path=(
                        _sl_config.store_path
                        / const.SYNC_RECORDS_REL_PATH
                        / _bm.index_name
                        / f"{_part.value}.rec"
                    ).as_posix(),
                )
                if not _exists:
                    continue  # vanished between the listing and the read
                try:
                    _remote_rec = _SyncRecord.model_validate_json(_raw)
                except Exception:
                    continue

                if not _remote_rec.sync_complete:
                    # The LOCAL record is complete but the remote one is not: a
                    # push from ANOTHER machine died half-way. No other check can
                    # see this -- `interrupted-sync` reads only local records --
                    # so it is exactly the kind of wedge that stays silent for
                    # months.
                    #
                    # A push in flight looks identical, so only a record older
                    # than the grace window is reported. Anything shorter would
                    # flag every long push on the fleet, and a report that cries
                    # wolf is one nobody reads.
                    _age = _now - _remote_rec.ulid.datetime
                    if _age < _INCOMPLETE_REMOTE_GRACE:
                        continue
                    _add_finding(
                        "diverged-box",
                        f"A push of the {_part.value} of '{_bm.index_name}' from "
                        f"'{_remote_rec.syncer_hostname}' never completed "
                        f"({_remote_rec.timestamp:%Y-%m-%d}, {_age.days}d ago), so the "
                        f"remote copy may be half-written",
                        f"Syncing here will refuse until it is resolved. Check whether "
                        f"'{_remote_rec.syncer_hostname}' still has the box, then re-run "
                        f"the push from whichever machine holds the good copy: `boxyard "
                        f"sync -r '{_bm.index_name}' --sync-direction push "
                        f"--sync-setting force --sync-choices {_part.value}`.",
                        index_name=_bm.index_name,
                        box_part=_part.value,
                        storage_location=_sl_name,
                    )
                    continue

                if _local_rec.ulid == _remote_rec.ulid:
                    continue  # the prefilter was merely cautious

                # The records disagree. Whether the LOCAL side has also moved is
                # what separates a real conflict from a plain needs-pull. This
                # mirrors `get_sync_status` exactly -- same exclude-aware scan,
                # same comparison -- so doctor and sync cannot disagree.
                _conf_exclude = (
                    _bm.get_local_part_path(config, BoxPart.CONF)
                    / const.RCLONE_EXCLUDE_FILENAME
                )
                _local_modified = check_last_time_modified(
                    _bm.get_local_part_path(config, _part),
                    exclude_names=literal_exclude_names(
                        _conf_exclude
                        if _conf_exclude.exists()
                        else config.default_rclone_exclude_path
                    ),
                )
                _local_changed = (
                    _local_modified is not None
                    and _local_modified > _local_rec.timestamp
                )
                _remote_newer = _remote_rec.ulid.datetime > _local_rec.ulid.datetime
                if _remote_newer and not _local_changed:
                    continue  # NEEDS_PULL -- the next sync resolves it

                _add_finding(
                    "diverged-box",
                    f"The {_part.value} of '{_bm.index_name}' has diverged: local record "
                    f"{_local_rec.timestamp:%Y-%m-%d} from '{_local_rec.syncer_hostname}', "
                    f"remote record {_remote_rec.timestamp:%Y-%m-%d} from "
                    f"'{_remote_rec.syncer_hostname}'"
                    + (" and the local copy has changed since" if _local_changed else ""),
                    f"Both sides moved on independently, so sync refuses rather than pick a "
                    f"winner. Look before choosing: `boxyard box-status -r "
                    f"'{_bm.index_name}'`, and compare against the remote with `boxyard copy "
                    f"-r '{_bm.index_name}' -d /tmp/compare`. Then resolve with an explicit "
                    f"`--sync-direction` and `--sync-setting force`.",
                    index_name=_bm.index_name,
                    box_part=_part.value,
                    storage_location=_sl_name,
                )

# %% [markdown]
# ## Check: `tree-orphans`
#
# Boxmeta `parents` entries referencing box ids that are unknown locally.
# `boxyard tree` shows these under `[unknown parent]`.

# %%
#|export
_known_box_ids = {bm.box_id for bm in box_metas}

for bm in box_metas:
    for _parent_id in bm.parents:
        if _parent_id not in _known_box_ids:
            _add_finding(
                "tree-orphans",
                f"Box '{bm.index_name}' references unknown parent box id '{_parent_id}'",
                "Fetch missing metas with `boxyard sync-missing-meta`, or drop the stale parent with `boxyard remove-parent`.",
                index_name=bm.index_name,
                parent_box_id=_parent_id,
            )

# %% [markdown]
# ## Check: `unknown-boxmeta-keys`
#
# A `boxmeta.toml` written by a NEWER boxyard than this one. `BoxMeta.load`
# keeps such keys verbatim rather than rejecting the file, because rejecting it
# does not merely fail to parse: `create_boxyard_meta` skips the registration,
# so the box disappears from `boxyard_meta.json`, from `boxyard list`, from
# `~/g` (its symlinks are deleted) and from `multi-sync` — it stops syncing
# with no error, and upgrading afterwards does not heal it.
#
# Tolerating the key is what prevents that; this check is what stops the
# tolerance from being silent. Purely local — the keys are already on disk.

# %%
#|export
import importlib.metadata as _importlib_metadata

try:
    _running_version = _importlib_metadata.version("boxyard")
except _importlib_metadata.PackageNotFoundError:
    # Running from a source checkout with no installed distribution. Say
    # nothing about the version rather than inventing one.
    _running_version = None
_running_suffix = f" (running {_running_version})" if _running_version else ""

for bm in box_metas:
    if not bm.unknown_keys:
        continue
    _keys = ", ".join(sorted(bm.unknown_keys))
    _add_finding(
        "unknown-boxmeta-keys",
        f"Box '{bm.index_name}' has boxmeta key(s) this boxyard does not know: {_keys}",
        f"The box was written by a newer boxyard. The key(s) are preserved "
        f"untouched, so nothing is lost and there is nothing to repair — but this "
        f"machine cannot act on what they mean. Upgrade boxyard here"
        f"{_running_suffix} to the version that writes them.",
        index_name=bm.index_name,
        storage_location=bm.storage_location,
        unknown_keys=sorted(bm.unknown_keys),
    )

# %% [markdown]
# ## Check: `machine-name-unset`
#
# `machine_name` is how a machine identifies itself for box write-ownership.
# It is configured and never derived: `get_hostname()` cannot serve as an
# identity — one machine in this fleet has reported both `lukas-pocket4` and
# `pocket4`, and macOS reports user-editable pretty names like
# `Lukas’s MacBook Pro`.
#
# Reported whenever it is unset, not only once boxes are owned: an unnamed
# machine can never claim a box, and the point of reporting it is to make the
# gap between installing this version and configuring the name visible while
# it is still true, rather than at the moment someone first tries to claim.

# %%
#|export
if config.machine_name is None:
    _add_finding(
        "machine-name-unset",
        f"No `machine_name` is configured in '{config.config_path}'",
        f"Nothing is broken by this today — box write-ownership is not yet "
        f"enforced — but this machine cannot own a box until it has a name. Set "
        f"`machine_name` to this machine's canonical short name (the same one "
        f"myrig uses, e.g. 'macbook' or 'mymain') in '{config.config_path}', or "
        f"export {const.ENV_VAR_BOXYARD_MACHINE_NAME} for a one-off.",
        config_path=config.config_path,
    )

# %% [markdown]
# ## Check: `unknown-config-keys`
#
# `config.toml` carries a key this version does not know. Since v0.5.0 that no
# longer makes every command on the machine fail — `get_config` collects such
# keys instead of rejecting the file — and this check is the other half of that
# bargain.
#
# It matters more than it looks. `extra="forbid"` is what catches a TYPO'd
# config key today; tolerating unknown keys without reporting them would trade
# a loud typo for a silent one, which is a worse deal than the one being
# fixed. So every key that lands in the passthrough is named here, whether it
# came from a newer boxyard or from a slip of the fingers — doctor cannot tell
# the two apart, and the hint says so rather than guessing.
#
# Keys are reported by their dotted path, so a key inside a table names the
# entry it is in (`storage_locations.hetzner-box.some_key`) rather than leaving
# the reader to search the file for it.

# %%
#|export
if config.unknown_keys:
    _config_keys = ", ".join(sorted(config.unknown_keys))
    _add_finding(
        "unknown-config-keys",
        f"Config '{config.config_path}' has key(s) this boxyard does not know: "
        f"{_config_keys}",
        f"They are ignored, not fatal. Either the config was written for a newer "
        f"boxyard -- upgrade this machine{_running_suffix} -- or the key is a typo, "
        f"in which case whatever it was meant to configure is silently not in "
        f"effect. Check the spelling against `boxyard init`'s generated config "
        f"before assuming the former.",
        config_path=config.config_path,
        unknown_keys=sorted(config.unknown_keys),
    )

# %% [markdown]
# ## Check: `stale-owner`
#
# A box whose `write_owner` cannot be a working owner — meaning NO machine can
# push it, and the only way back is `--steal` from somewhere. Purely local.
#
# Two sub-cases, and they are not equally certain:
#
# - **Owned by this machine, but not included here.** Exact, with no false
#   positives: this machine is the designated writer of DATA it does not hold.
#   `claim` now refuses a box that is not included, and `exclude` releases a box
#   it owns, so both known routes into this are closed — but they were both
#   found by inspection rather than by anything reporting them, which is the
#   whole reason this check exists.
# - **Owned by a name that owns exactly one box, while some other machine owns
#   several.** A heuristic, and labelled as one. A real machine in a migrated
#   yard owns tens to hundreds of boxes, so a name holding exactly one — in a
#   yard where another name holds more — is far more likely a machine that was
#   renamed or retired than a machine with one box. The "some other machine owns
#   several" condition is what keeps this quiet during the migration itself,
#   when the first machine to claim is legitimately the only owner in the yard.
#
# Note what is deliberately NOT reported: a box owned by another machine that is
# not included here. That is the ordinary state of most boxes on most machines.

# %%
#|export
_owner_counts: dict[str, int] = {}
for bm in box_metas:
    if bm.write_owner is not None:
        _owner_counts[bm.write_owner] = _owner_counts.get(bm.write_owner, 0) + 1

# Is there a machine in this yard that owns more than one box? Until there is,
# "owns exactly one box" says nothing.
_yard_has_an_established_owner = any(count > 1 for count in _owner_counts.values())

for bm in box_metas:
    if bm.write_owner is None:
        continue
    if bm.box_id in _placement_problem_ids:
        continue

    if bm.write_owner == config.machine_name:
        _owner_checkout = get_box_checkout_status(config, bm)
        if _owner_checkout.state == LocalCheckoutState.UNAVAILABLE:
            continue  # still included; checkout-root-unavailable already explains why it cannot push now
        if _owner_checkout.state not in (
            LocalCheckoutState.INCLUDED,
            LocalCheckoutState.RELOCATING,
        ):
            _add_finding(
                "stale-owner",
                f"Box '{bm.index_name}' names this machine "
                f"('{config.machine_name}') as its write owner, but it is not "
                f"included here as a complete checkout (state "
                f"'{_owner_checkout.state.value}') — so the one machine allowed "
                f"to push it does not have complete DATA",
                f"No machine can push this box until that is fixed. Either give it "
                f"up with `boxyard release -r '{bm.index_name}'`, or take the box "
                f"back with `boxyard include -r '{bm.index_name}'`.",
                index_name=bm.index_name,
                write_owner=bm.write_owner,
                storage_location=bm.storage_location,
            )
        continue

    if _yard_has_an_established_owner and _owner_counts[bm.write_owner] == 1:
        _add_finding(
            "stale-owner",
            f"Box '{bm.index_name}' is owned by '{bm.write_owner}', which owns no "
            f"other box in this yard",
            f"Probably a machine that was renamed or retired, in which case no "
            f"machine can push this box. If '{bm.write_owner}' is real and simply "
            f"owns only this box, nothing is wrong. Otherwise take it over from "
            f"the machine that should have it: `boxyard claim --steal -r "
            f"'{bm.index_name}'`.",
            index_name=bm.index_name,
            write_owner=bm.write_owner,
            storage_location=bm.storage_location,
        )

# %% [markdown]
# ## Check: `write-denied`
#
# A box owned by another machine that has local changes here which will never be
# pushed. **This is the only report of that state.** The sync path deliberately
# says nothing — see `SyncCondition.WRITE_DENIED`, and the ~72 supervisor passes
# per machine per day that would otherwise each produce an identical error — so
# a state doctor does not surface is a state nobody ever sees.
#
# Cost is the deciding constraint, as it was for `diverged-box`. The expensive
# question ("would a push actually transfer anything?") is asked only of boxes
# that pass two free local filters first: owned by someone else AND included
# here AND locally modified since their own sync record. On a machine where
# every box is owned correctly, that is zero remote calls.
#
# The modification test is the sync engine's own exclude-aware scan, so doctor
# and sync cannot disagree about whether a box has changed.

# %%
#|export
if not check_remote or not _rclone_available:
    checks["write-denied"]["skipped"] = True
else:
    from boxyard._models import BoxPart as _BoxPart, SyncRecord as _SyncRec
    from boxyard._ownership import (
        push_would_transfer,
        write_denied_hint,
        write_denied_message,
    )
    from boxyard._utils import check_last_time_modified, literal_exclude_names

    for bm in box_metas:
        if bm.write_owner is None or bm.write_owner == config.machine_name:
            continue
        if bm.box_id in _placement_problem_ids:
            continue
        _sl_config = config.storage_locations.get(bm.storage_location)
        if _sl_config is None or _sl_config.storage_type != StorageType.RCLONE:
            continue
        if storage_locations is not None and bm.storage_location not in storage_locations:
            continue
        if not bm.check_included(config):
            continue  # not here at all, so nothing of ours can be stranded

        _data_path = bm.get_local_part_path(config, _BoxPart.DATA)
        _conf_exclude = (
            bm.get_local_part_path(config, _BoxPart.CONF) / const.RCLONE_EXCLUDE_FILENAME
        )
        _effective_exclude = (
            _conf_exclude if _conf_exclude.exists() else config.default_rclone_exclude_path
        )

        _rec_path = bm.get_local_sync_record_path(config, _BoxPart.DATA)
        if not _rec_path.exists():
            continue  # never synced here; `interrupted-sync`/`stale-cache` territory
        try:
            _rec = _SyncRec.model_validate_json(_rec_path.read_text())
        except Exception:
            continue  # a malformed record is `interrupted-sync`'s business

        _modified = check_last_time_modified(
            _data_path, exclude_names=literal_exclude_names(_effective_exclude)
        )
        if _modified is None or _modified <= _rec.timestamp:
            continue  # unchanged since our own record: nothing is stranded

        # Only now is a remote call worth making. `needs_push` is not evidence
        # of a real change -- a single `.DS_Store` sets it -- so ask what a push
        # would ACTUALLY move, under the box's real filters.
        _remote_data_path = (
            _sl_config.store_path
            / const.REMOTE_BOXES_REL_PATH
            / bm.index_name
            / const.BOX_DATA_REL_PATH
        )
        _conf_path = bm.get_local_part_path(config, _BoxPart.CONF)
        _include_file = _conf_path / const.RCLONE_INCLUDE_FILENAME
        _filters_file = _conf_path / const.RCLONE_FILTERS_FILENAME

        if not await push_would_transfer(
            config,
            local_path=_data_path,
            remote=bm.storage_location,
            remote_path=_remote_data_path,
            include_path=_include_file if _include_file.exists() else None,
            exclude_path=_effective_exclude,
            filters_path=_filters_file if _filters_file.exists() else None,
        ):
            continue

        _add_finding(
            "write-denied",
            write_denied_message(config, bm)
            + " This copy has local changes that will never leave this machine.",
            write_denied_hint(config, bm),
            index_name=bm.index_name,
            write_owner=bm.write_owner,
            storage_location=bm.storage_location,
        )

# %% [markdown]
# ## Check: `unpushed-meta-edit`
#
# A `boxmeta.toml` that differs from the copy this machine last agreed with the
# remote about, with no push since.
#
# On its own that is an ordinary pending edit, not a fault — `add-to-group`,
# `set-parent` and friends do not push unless asked (`--sync-after`). The point
# is the TIMING. While the edit sits unpushed it is one push by any other
# machine away from becoming a two-sided divergence, and a two-sided divergence
# is a dead end: sync refuses, and nothing but a human picking a winner
# resolves it.
#
# That is not hypothetical. On 2026-08-25, forty-four boxes on macbook were
# given an `archived` or `dormant` group locally; over the same afternoon the
# other machines ran the v0.5.x ownership claim sweep, which writes
# `write_owner` into `boxmeta.toml` and pushes. Every one of the forty-four
# became a conflict, they stopped propagating their groups entirely, and every
# machine except macbook reported "all checks passed" throughout.
#
# Purely local and free: it compares two files already on disk. It says nothing
# about a box whose META has not synced since the merge base was introduced,
# because there is nothing to compare against — an absence, not a fault.

# %%
#|export
from boxyard._models import read_meta_base

for bm in box_metas:
    _base = read_meta_base(config, bm)
    if _base is None:
        continue

    # Compare the FIELDS, not the file bytes. A boxmeta rewritten with the same
    # content -- reordered keys, a trailing newline -- is not an edit, and
    # reporting it would train the reader to ignore this check.
    _changed = [
        _field
        for _field in ("groups", "parents", "write_owner")
        if getattr(bm, _field) != getattr(_base, _field)
    ]
    if not _changed:
        continue

    _add_finding(
        "unpushed-meta-edit",
        f"Box '{bm.index_name}' has local metadata changes ({', '.join(_changed)}) "
        f"that have not been pushed",
        f"Harmless until another machine pushes this box's META, at which point "
        f"it becomes a divergence that sync refuses. Push it with `boxyard sync "
        f"-r '{bm.index_name}' --sync-choices meta`.",
        index_name=bm.index_name,
        changed_fields=_changed,
        storage_location=bm.storage_location,
    )

# %% [markdown]
# ## Check: `unowned-box`
#
# A box included here that no machine has claimed.
#
# Unowned means unrestricted, so nothing is being withheld. What it also means
# is that two machines can push the same box and diverge — the exact problem
# ownership exists to remove — and nothing surfaced it: `include` prints a
# one-line nudge, and if you were not running `include` you never heard about
# it again.
#
# That gap had a measurable shape. `new_box` never set `write_owner`, so every
# box created since ownership landed was born unowned; on mymain on 2026-08-27
# the ONLY unowned boxes held there were the three created since the claim
# sweep. `boxyard new` claims from v0.5.17, which closes the source; this
# reports the ones already made.
#
# Scoped to boxes INCLUDED here, for the same reason `claim` refuses a box that
# is not: a machine that does not hold a box cannot become its owner, so a
# finding about one would name a command that fails. This machine sees ~590
# boxmetas and holds ~120; reporting all of them would be noise nobody reads.

# %%
#|export
for bm in box_metas:
    if bm.write_owner is not None:
        continue
    if not bm.check_included(config):
        continue
    _add_finding(
        "unowned-box",
        f"Box '{bm.index_name}' is included here but no machine has claimed it",
        f"Nothing is blocked — unowned means unrestricted — but two machines "
        f"can push it and diverge. If this is where you work on it: `boxyard "
        f"claim -r '{bm.index_name}'`.",
        index_name=bm.index_name,
        storage_location=bm.storage_location,
    )

# %% [markdown]
# Assemble the report.

# %%
#|export
num_findings = sum(len(check["findings"]) for check in checks.values())
report = {
    "healthy": num_findings == 0,
    "num_findings": num_findings,
    "checks": checks,
}

# %%
#|func_return
report

# %% [markdown]
# ## Inspect the report

# %%
import json

print(json.dumps(report, indent=2))

# %%
_findings_by_check = {name: check["findings"] for name, check in report["checks"].items()}

assert not report["healthy"]

# The hand-made folder is flagged both as unregistered and as malformed
assert any(
    f.get("path", "").endswith("hand-made-folder")
    for f in _findings_by_check["unregistered-folder"]
)
assert any(
    f.get("path", "").endswith("hand-made-folder")
    for f in _findings_by_check["malformed-name"]
)

# The dangling group symlink is found
assert any(
    f.get("path", "").endswith("ghost-box") for f in _findings_by_check["dangling-symlinks"]
)

# The orphaned sync-record directory is found
assert any(
    f.get("path", "").endswith("20990101_zzzzzz__ghost-box")
    for f in _findings_by_check["orphaned-sync-records"]
)

# The real file in the group tree is found
assert any(
    f.get("path", "").endswith("debris.txt") for f in _findings_by_check["group-tree-debris"]
)

# The interrupted sync of box1 is found
assert any(
    f.get("index_name") == box_index_name1 for f in _findings_by_check["interrupted-sync"]
)

# The unknown storage-location dir is found
assert any(
    f.get("path", "").endswith("ghost-storage")
    for f in _findings_by_check["unknown-storage-location"]
)

# The foreign remote box is reported as missing from the local meta mirror
assert any(
    _foreign_index_name in f.get("missing_index_names", [])
    for f in _findings_by_check["stale-meta-mirror"]
)

# The tombstoned box is found (box2 is deleted on the remote but registered locally)
assert any(
    f.get("index_name") == box_index_name2 for f in _findings_by_check["tombstoned-box"]
)

# The properly created boxes cause no findings
assert not _findings_by_check["broken-registration"]
assert not _findings_by_check["stale-cache"]
assert not _findings_by_check["tree-orphans"]
assert not _findings_by_check["duplicate-box-id"]
assert not _findings_by_check["rclone-config"]

# %%
# With check_remote=False the remote checks are skipped and the offline report still works
from boxyard.cmds._doctor import run_doctor as _run_doctor

offline_report = await _run_doctor(config_path=config_path, check_remote=False)
assert offline_report["checks"]["stale-meta-mirror"]["skipped"]
assert offline_report["checks"]["tombstoned-box"]["skipped"]
assert not any(
    f["message"].startswith("Could not list")
    for f in offline_report["checks"]["stale-meta-mirror"]["findings"]
)

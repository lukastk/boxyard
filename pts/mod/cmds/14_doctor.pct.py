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
# 7. **orphaned-sync-records** — `sync_records/<index>/` with no matching local registration.
# 8. **stale-meta-mirror** — per rclone storage location, remote boxmetas not present
#    locally (what `sync-missing-meta` would fetch). Skippable so doctor works offline.
# 9. **tree-orphans** — boxmeta parents referencing unknown box ids.
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
    "orphaned-sync-records",
    "stale-meta-mirror",
    "tree-orphans",
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
            (the stale-meta-mirror check), so doctor works offline.
        storage_locations: If given, restrict the stale-meta-mirror check to
            these storage locations. Local checks always cover all storage
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

# A remote box that is not mirrored locally (as if created by another machine)
import toml as _toml

_foreign_index_name = "20990101_aaaaaa__foreign-box"
_foreign_box_path = remote_rclone_path / "boxyard" / const.REMOTE_BOXES_REL_PATH / _foreign_index_name
_foreign_box_path.mkdir(parents=True)
(_foreign_box_path / const.BOX_METAFILE_REL_PATH).write_text(
    _toml.dumps({"storage_location": "my_remote", "creator_hostname": "other-machine", "groups": [], "parents": []})
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
# ## Check: `unregistered-folder`
#
# Every directory inside `user_boxes_path` must be a registered box. Folders
# hand-created directly in `user_boxes_path` (instead of via `boxyard new`)
# are invisible to boxyard and will never sync.

# %%
#|export
all_registration_dirs = set().union(*registration_dirs_by_sl.values())

if config.user_boxes_path.is_dir():
    for entry in sorted(config.user_boxes_path.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if not entry.is_dir():
            _add_finding(
                "unregistered-folder",
                f"Stray file in user boxes path: '{entry}'",
                f"Only box directories belong in '{config.user_boxes_path}'; move the file into a box or delete it.",
                path=entry,
            )
            continue
        if entry.name not in registered_index_names:
            _add_finding(
                "unregistered-folder",
                f"Directory '{entry.name}' in '{config.user_boxes_path}' is not a registered box",
                f"Register it with `boxyard new --from '{entry}' -n <name>` (moves it into a new box), or move it out of '{config.user_boxes_path}'.",
                path=entry,
            )

# %% [markdown]
# ## Check: `malformed-name`
#
# Entries in `user_boxes_path` whose names don't parse as a valid index name.
# A malformed name can never correspond to a registration, so these entries are
# usually also flagged as `unregistered-folder` — the two findings are distinct
# problems (the folder is untracked; its name will never parse).

# %%
#|export
if config.user_boxes_path.is_dir():
    for entry in sorted(config.user_boxes_path.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if not is_valid_index_name(
            entry.name, config.box_subid_character_set, config.box_subid_length
        ):
            _add_finding(
                "malformed-name",
                f"Directory name '{entry.name}' does not parse as an index name '<timestamp>_<subid>__<name>'",
                "Boxes must be created via `boxyard new`, which generates the index name; rename/move the folder or register it with `boxyard new --from`.",
                path=entry,
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

for _box_id, _bms in sorted(_metas_by_id.items()):
    if len(_bms) > 1:
        _locations = ", ".join(f"{bm.storage_location}/{bm.index_name}" for bm in _bms)
        _add_finding(
            "duplicate-box-id",
            f"Box id '{_box_id}' is registered {len(_bms)} times: {_locations}",
            "Box ids must be unique; inspect the duplicates and delete or re-create one of them.",
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
# ## Check: `dangling-symlinks`
#
# Symlinks under `user_box_groups_path` whose targets don't exist.

# %%
#|export
def _check_symlinks(path: Path) -> None:
    # Explicit walk that never descends into symlinks — Path.rglob follows
    # directory symlinks on Python < 3.13, which would scan inside every box.
    for entry in sorted(path.iterdir(), key=lambda p: p.name):
        if entry.is_symlink():
            if not entry.exists():
                _add_finding(
                    "dangling-symlinks",
                    f"Symlink '{entry}' points to a non-existent target '{entry.readlink()}'",
                    "Run `boxyard create-user-symlinks` to rebuild the group symlinks.",
                    path=entry,
                )
        elif entry.is_dir():
            _check_symlinks(entry)


if config.user_box_groups_path.is_dir():
    _check_symlinks(config.user_box_groups_path)

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
# ## Check: `stale-meta-mirror`
#
# Per rclone storage location, list the remote boxmetas and report the ones not
# present in the local meta mirror — exactly what `boxyard sync-missing-meta`
# would fetch. A machine where this never runs silently hides newer boxes from
# `boxyard list` and everything built on it. Skipped when `check_remote` is
# False so doctor works offline. A failure to list the remote is reported
# loudly as a finding rather than being treated as "no boxes".

# %%
#|export
if not check_remote:
    checks["stale-meta-mirror"]["skipped"] = True
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
            if _ls_root is None:
                _add_finding(
                    "stale-meta-mirror",
                    f"Could not list remote storage location '{sl_name}'",
                    "Check connectivity and the rclone config, or run doctor with --no-remote to skip remote checks.",
                    storage_location=sl_name,
                )
            continue

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

# The foreign remote box is reported as missing from the local meta mirror
assert any(
    _foreign_index_name in f.get("missing_index_names", [])
    for f in _findings_by_check["stale-meta-mirror"]
)

# The properly created boxes cause no findings
assert not _findings_by_check["broken-registration"]
assert not _findings_by_check["stale-cache"]
assert not _findings_by_check["tree-orphans"]
assert not _findings_by_check["duplicate-box-id"]

# %%
# With check_remote=False the remote check is skipped and the offline report still works
from boxyard.cmds._doctor import run_doctor as _run_doctor

offline_report = await _run_doctor(config_path=config_path, check_remote=False)
assert offline_report["checks"]["stale-meta-mirror"]["skipped"]
assert not any(
    f["message"].startswith("Could not list")
    for f in offline_report["checks"]["stale-meta-mirror"]["findings"]
)

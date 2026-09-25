# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Remote record identity
#
# "Has the remote record of this part changed since this machine last agreed
# with it?" is the remote half of the full-pass skip
# (`_dev/FULL-PASS-SKIP-DESIGN-NOTE.md`). The remote half of the answer comes
# from ONE bulk `rclone lsjson --hash` over `sync_records/`: the md5 of every
# `<part>.rec` file, computed by the storage box itself. The local half is a
# machine-local sidecar beside the local record, `<part>.remote.json`, holding
# the md5 of the remote record's bytes as this machine last READ them on the
# real sync path -- when the verdict was SYNCED or EXCLUDED, or when a push or
# pull completed.
#
# Nothing is written to the remote for this. The identity is the record's own
# content, which every writer -- any version of boxyard -- changes by writing a
# new record; there is no second file to keep consistent, no crash ordering to
# reason about, and no bootstrap: the first real-path pass after an upgrade
# writes the sidecars, and the second pass can skip. The v3/v4 design published
# a zero-byte "marker" beside each remote record and was withdrawn after its
# implementation review reproduced three ways that marker could name a record
# the remote no longer held.
#
# A sidecar is a FACT, never a decision: "these bytes were a complete record
# with this ULID". A stale sidecar can only ever fail to match, because the
# remote's current md5 is compared against it on every pass.

# %%
#|default_exp _remote_identity

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import hashlib
import json
import os
from pathlib import Path

from boxyard import const

REMOTE_IDENTITY_VERSION = 1


def record_md5(text: str) -> str:
    """
    md5 of a sync record's bytes, as `rclone lsjson --hash` reports it for the
    file on the remote. Records are UTF-8 JSON and `run_cmd_async` decodes
    stdout as UTF-8 without stripping, so re-encoding the text reproduces the
    file's bytes exactly.
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def remote_identity_path(local_sync_record_path: "str | Path") -> Path:
    """`<dir>/data.rec` -> `<dir>/data.remote.json`, beside the record it describes."""
    p = Path(local_sync_record_path)
    return p.with_name(p.name.removesuffix(".rec") + const.BOX_REMOTE_IDENTITY_SUFFIX)


def write_remote_identity(
    local_sync_record_path: "str | Path",
    *,
    md5: str,
    ulid: str,
    sync_complete: bool,
) -> Path:
    """
    Remember the remote record this machine just agreed with. Atomic (temp
    file plus `os.replace`), like the fingerprint sidecar: a torn sidecar that
    happened to parse would be the one dangerous outcome.
    """
    p = remote_identity_path(local_sync_record_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": REMOTE_IDENTITY_VERSION,
        "md5": md5,
        "ulid": str(ulid),
        "sync_complete": bool(sync_complete),
    }
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
    return p


def read_remote_identity(local_sync_record_path: "str | Path") -> "dict | None":
    """
    The remembered remote identity, or None when there is not a usable one.
    Absence, corruption, a version bump and a missing field all read as None:
    every one of them means "cannot prove", which costs a real check.
    """
    p = remote_identity_path(local_sync_record_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != REMOTE_IDENTITY_VERSION:
        return None
    if not isinstance(data.get("md5"), str) or not isinstance(data.get("ulid"), str):
        return None
    if not isinstance(data.get("sync_complete"), bool):
        return None
    return data


def clear_remote_identity(local_sync_record_path: "str | Path") -> None:
    """Remove the sidecar. Absence is fine -- used on teardown paths."""
    remote_identity_path(local_sync_record_path).unlink(missing_ok=True)


def note_agreement(local_sync_record_path: "str | Path", status) -> bool:
    """
    Write the sidecar from a `SyncStatus` whose verdict means "nothing to do":
    SYNCED (both sides hold the same record and the local tree matches it) or
    EXCLUDED (this machine deliberately holds no copy; the remote record was
    read and is complete -- an incomplete one yields an INCOMPLETE condition
    before EXCLUDED is ever reached). Any other verdict writes nothing and
    returns False. A status with no remote record (never pushed) has no
    identity to remember and also returns False.
    """
    from boxyard._models import SyncCondition

    if status.sync_condition not in (SyncCondition.SYNCED, SyncCondition.EXCLUDED):
        return False
    rec = status.remote_sync_record
    md5 = status.remote_sync_record_md5
    if rec is None or md5 is None:
        return False
    write_remote_identity(
        local_sync_record_path,
        md5=md5,
        ulid=str(rec.ulid),
        sync_complete=rec.sync_complete,
    )
    return True

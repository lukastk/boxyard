# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _fingerprint
#
# Whether a box's local tree still matches what this machine last synced.
#
# ## The question the old code asked, and why it was the wrong one
#
# `get_sync_status` used to decide "has this box changed?" by taking the NEWEST
# MTIME under the box and comparing it against the sync record's timestamp. That
# is a proxy, and it only detects changes that leave a newer file mtime behind.
# Measured, on a real tree:
#
#     SEEN       edit a file's content
#     SEEN       add a new file
#     INVISIBLE  delete a file
#     INVISIBLE  rename a file
#     INVISIBLE  chmod +x
#     INVISIBLE  delete a whole directory
#     INVISIBLE  restore a file with a preserved mtime (cp -p, rsync -a, tar -x)
#     INVISIBLE  add a symlink
#     INVISIBLE  remove a symlink
#     INVISIBLE  retarget a symlink
#
# Eight of ten invisible. Confirmed against the live remote, not just in theory:
# on a 16,746-file box, adding one file synced in 19s; deleting one file synced
# in 8s -- exactly no-op time -- and the deleted file was still on the remote
# afterwards. The two copies disagreed permanently and nothing reported it.
#
# It self-heals by accident: any LATER change that does bump a file's mtime
# carries the pending one out with it. So what is actually lost is a change that
# happens ALONE.
#
# The symlink rows are their own defect: every sync passes `--links`, so
# symlinks transfer as `.rclonelink` files, but the mtime walk classifies with
# `is_file(follow_symlinks=False)` / `is_dir(follow_symlinks=False)`, which are
# BOTH false for a symlink -- so symlinks were never looked at at all.
#
# ## What replaces it
#
# A FINGERPRINT of the tree, compared against the fingerprint recorded when this
# machine last completed a sync. Not a timestamp -- the actual set.
#
# The elegance argument is that this is not a new mechanism, it is the
# transport's own equality relation written down. rclone decides what to
# transfer by `(path, size, modtime)` -- boxyard passes no `--checksum`
# anywhere -- so a digest over exactly those tuples is precisely "what rclone
# would consider different". A completed sync is the one moment local and remote
# are known equal, so the recorded fingerprint is a LOCAL CACHE OF THE REMOTE'S
# STATE under the transport's own metric. That is why the question becomes
# answerable with zero remote calls, which matters because listing the remote is
# the expensive operation (it tracks DIRECTORY count, ~80/s over SFTP here).
#
# It is also the analogue of what the restic backend already does -- compare the
# tree against the snapshot's node list. Plain gains the missing half rather
# than a second, different mechanism.
#
# ## Why not simply stat directories too
#
# It is three lines and it detects every shape above. It is also wrong, and the
# repo has already paid to learn why: an EXCLUDED file appearing in a directory
# (macOS writing `.DS_Store`) still moves that DIRECTORY's mtime. That flips a
# box to NEEDS_PUSH with nothing transferable changed, and -- when the remote has
# also moved on -- to CONFLICT, which is how a box wedges needing manual
# resolution. `check_last_time_modified`'s exclude filtering exists to prevent
# exactly that.
#
# The restic gate (`tree_touched_since`) CAN stat directories because being
# wrong towards "maybe" costs it one extra local check. Here the value directly
# drives sync status, so a false positive is a wedged box, not a wasted call.
#
# ## Why the fingerprint is machine-local, and cannot live in the sync record
#
# Two independent reasons, either sufficient:
#
# 1. After a PULL the local record is a VERBATIM COPY of the remote one
#    (`_utils/02_sync_helper.pct.py`, the pull branch reads the remote record and
#    saves it locally). A fingerprint inside it would be the PUSHER's, adopted by
#    the PULLER, whose files have different mtimes after download -- permanent
#    spurious NEEDS_PUSH on every pulled box.
# 2. `SyncRecord` extends `StrictModel`, which is `extra="forbid"`. A new field
#    would make `model_validate_json` RAISE on every machine that had not
#    upgraded yet, for every box, every pass -- forcing a lockstep fleet upgrade.
#
# So it is a sidecar beside the local record, and nothing new crosses the wire.
# `meta.base.toml` is the exact precedent: machine-local "what this machine last
# agreed with", living in the same directory, deliberately never travelling.
#
# ## Why it is bound to a ULID
#
# A bare digest can be stale -- a crash between writing the record and writing
# the sidecar, a `force-push`, a `convert`, hand surgery. A stale fingerprint is
# the one dangerous failure mode, because it can say "unchanged" about a tree
# that changed. Binding it to the record's ULID collapses every one of those
# cases into the single well-defined "no usable baseline" case, which is loud.
#
# ## What it deliberately does NOT hash
#
# **Full `st_mode`.** The only mode information boxyard can actually transport is
# the owner-execute bit, carried out-of-band in `.boxyard-perms.json` because the
# transport drops Unix mode. Hashing all of `st_mode` would make `chmod g+w` --
# untransportable by any mechanism here -- flip a box to NEEDS_PUSH forever
# hunting a change that cannot be propagated. So the fingerprint records exactly
# the bit the perms manifest records, and "fingerprint-visible mode change" is
# by construction the same set as "manifest-visible mode change".
#
# **Directories.** rclone does not sync empty directories, so including them
# would reintroduce the `.DS_Store` false positive through the back door.
#
# ## The filter policy is part of the fingerprint
#
# Hashing only the resulting file set is not enough, because the SET can be
# unchanged while the SCOPE changes. rclone does not delete excluded files from
# the destination, so:
#
#   1. `foo` is excluded; absent locally, still present remotely.
#   2. Someone removes the exclusion.
#   3. The local visible set is unchanged -- `foo` was never there locally.
#   4. A tree-only digest is unchanged, so no sync runs, and the remote `foo`
#      is never reconciled into scope.
#
# So the active filter rules are hashed into the fingerprint alongside the tree.
# Editing them changes the fingerprint and forces exactly one reconcile.
#
# ## v2: the transport enumerates (2026-09-03)
#
# v1 approximated the exclude list with `literal_exclude_names` -- a Python
# walk skipping entries by literal NAME. Measured, that erred on BOTH sides:
# a glob-excluded file (`.git/**`) landed in the digest, so every git
# operation in such a box churned a no-op push cycle; and a regular file
# named like a dir-only pattern (`target/` excludes DIRECTORIES; a FILE named
# `target` syncs) was invisible, so its lone changes were never detected --
# the exact bug class this module exists to kill, surviving inside the fix.
#
# v2 enumerates with `rclone lsf` under the SAME exclude file the transfer
# uses, so "in the digest" and "would be transferred" are one set by
# construction. The rclone command builder refuses to mix filter families, so
# the exclude file is the whole filter policy a syncable box can have -- which
# is also why `filter_signature` signs only it.

# %%
#|default_exp _fingerprint

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import csv
import hashlib
import io
import json
import os
import stat as stat_mod
import subprocess
from pathlib import Path

from boxyard import const


FINGERPRINT_VERSION = 2
"""Bumped when the digest's INPUTS change, so old sidecars read as unusable
rather than being compared against a digest computed a different way.

v2 (2026-09-03): the tree is enumerated by rclone itself (`lsf` under the
box's real exclude file) instead of a Python walk over literal exclude names,
and the mtime is recorded as rclone's own max-precision string. Every v1
sidecar reads as unusable; the box falls back to the mtime test and regains a
v2 baseline through the ordinary convergence paths (bless-on-synced /
verify-then-bless)."""


# %%
#|export
def filter_signature(exclude_path: "str | Path | None") -> str:
    """
    A digest of the ACTIVE filter rules, so changing scope forces a reconcile.

    Blank lines and whole-line comments are normalised away, so reformatting a
    comment does not churn every box.

    Only the EXCLUDE file is signed, and that is now exact rather than a
    simplification: the rclone command builder refuses to combine filter
    families, so an exclude file is the only rule set a box can sync under.
    (Include/filters files gate the sync itself until the translation work on
    ticket 43f05498 -- this function once accepted them as parameters that no
    caller ever passed.)
    """
    parts: list[str] = []
    if exclude_path is not None:
        try:
            text = Path(exclude_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        rules = [
            ln.strip()
            for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if rules:
            parts.append("exclude:" + "\n".join(rules))
    digest = hashlib.sha256("\n--\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


# %%
#|export
_RCLONELINK_SUFFIX = ".rclonelink"


def _rclone_binary() -> str:
    from boxyard._utils.rclone import get_rclone_binary

    return get_rclone_binary()


def tree_fingerprint(
    path: "str | Path",
    *,
    rclone_config_path: "str | Path | None" = None,
    exclude_file: "str | Path | None" = None,
    filter_sig: str = "",
) -> "str | None":
    """
    A digest of everything under `path` that the transport would compare.

    Returns None ONLY when the root does not exist -- "there is no tree here",
    which the caller's existence logic must answer, not this function. An
    existing but EMPTY tree digests to a real, stable value, so emptying a box
    is a change like any other and its deletions propagate.

    THE TRANSPORT ENUMERATES. v1 walked the tree in Python and skipped
    entries whose NAME matched a literal exclude -- an approximation with
    errors on both sides, measured: a glob-excluded file landed in the digest
    (its churn read as a change -- a no-op push cycle on every git operation
    in a `.git/**`-excluded box), and a regular file named like a dir-only
    pattern (`target/` excludes directories; a FILE named `target` syncs) was
    invisible, so its lone changes were never detected -- the exact bug class
    this module exists to kill. Now `rclone lsf` lists the tree under the
    SAME exclude file the transfer uses, so "in the digest" and "would be
    transferred" are the same set by construction, glob or not.

    Per entry: the rclone-listed relative path, the kind, the size (for
    symlinks: the target string, which IS what rclone writes into the
    `.rclonelink` file), rclone's own max-precision mtime string, and -- for
    regular files -- the owner-execute bit from an lstat. An entry that
    vanishes between the listing and the stat keeps rclone's metadata: races
    read as changes (loud), never as absences.

    Entries are hashed individually and the digests sorted before the final
    hash, so memory stays bounded on a box with hundreds of thousands of
    files. A listing failure RAISES, exactly as the v1 walk raised on an
    unreadable directory and for the same reason: a shrunken enumeration
    hashes to a digest that says "unchanged" about files it never saw.
    """
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return None

    entry_digests: list[str] = []

    def record(rel: str, kind: str, size_or_target, mtime: str, execbit: str) -> None:
        line = f"{rel}\0{kind}\0{size_or_target}\0{mtime}\0{execbit}"
        entry_digests.append(
            hashlib.sha256(line.encode("utf-8", "surrogateescape")).hexdigest()
        )

    if root.is_file():
        st = root.lstat()
        record(root.name, "file", st.st_size, str(st.st_mtime_ns),
               "x" if st.st_mode & stat_mod.S_IXUSR else "-")
    else:
        cmd = [
            _rclone_binary(),
            "lsf",
            "--recursive",
            "--files-only",
            "--links",
            "--csv",
            "--format",
            "pst",
            "--time-format",
            "max",
        ]
        if rclone_config_path is not None:
            cmd += ["--config", str(rclone_config_path)]
        if exclude_file is not None and Path(exclude_file).is_file():
            cmd += ["--exclude-from", str(exclude_file)]
        cmd.append(str(root))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600
            )
        except subprocess.TimeoutExpired as e:
            raise OSError(
                f"Cannot fingerprint '{root}': the rclone listing timed out."
            ) from e
        if proc.returncode != 0:
            raise OSError(
                f"Cannot fingerprint '{root}': the rclone listing failed "
                f"({proc.stderr.strip()})."
            )

        for row in csv.reader(io.StringIO(proc.stdout)):
            if len(row) != 3:
                raise OSError(
                    f"Cannot fingerprint '{root}': unexpected rclone lsf row "
                    f"{row!r}."
                )
            listed_rel, size, mtime = row
            if listed_rel.endswith(_RCLONELINK_SUFFIX):
                rel = listed_rel[: -len(_RCLONELINK_SUFFIX)]
                try:
                    target = os.readlink(root / rel)
                except OSError:
                    # Raced away, or a REAL file carrying the suffix: rclone's
                    # metadata still identifies the entry deterministically.
                    target = f"<unreadable:{size}>"
                record(rel, "symlink", target, mtime, "-")
            else:
                try:
                    st = (root / listed_rel).lstat()
                    execbit = "x" if st.st_mode & stat_mod.S_IXUSR else "-"
                except OSError:
                    execbit = "-"
                record(listed_rel, "file", size, mtime, execbit)

    entry_digests.sort()
    h = hashlib.sha256()
    h.update(f"v{FINGERPRINT_VERSION}\0{filter_sig}\0".encode("utf-8"))
    for d in entry_digests:
        h.update(d.encode("ascii"))
    return h.hexdigest()


# %%
#|export
def base_path_for(local_sync_record_path: "str | Path") -> Path:
    """
    Where the sidecar lives, derived from the record it describes.

    Deriving it keeps `get_sync_status` a pure function of the paths it is
    already given, and makes all three parts (DATA, META, CONF) get the same
    treatment with no special-casing -- they all go through the same helper.
    """
    p = Path(local_sync_record_path)
    return p.with_name(p.name.removesuffix(".rec") + const.BOX_SYNC_BASE_SUFFIX)


def read_base(local_sync_record_path: "str | Path") -> "dict | None":
    """
    The recorded baseline, or None if there is not a usable one.

    Corruption, a version bump and absence all read as None -- the SAME
    "no usable baseline" answer -- because every one of them means the same
    thing operationally: this machine cannot prove what the tree looked like at
    the last sync, so it must go and find out. That is loud (it forces a
    reconcile), never silent.
    """
    p = base_path_for(local_sync_record_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != FINGERPRINT_VERSION:
        return None
    if not isinstance(data.get("fingerprint"), str):
        return None
    if not isinstance(data.get("sync_record_ulid"), str):
        return None
    return data


def write_base(
    local_sync_record_path: "str | Path",
    *,
    sync_record_ulid: str,
    fingerprint: str,
    filter_sig: str,
) -> Path:
    """
    Record the baseline atomically, bound to the record it describes.

    Atomic because a torn sidecar that happened to parse would be the one
    genuinely dangerous outcome -- a digest that says "unchanged" about a tree
    that changed. Temp file plus `os.replace`, the same way the check records
    are written.
    """
    p = base_path_for(local_sync_record_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": FINGERPRINT_VERSION,
        "sync_record_ulid": str(sync_record_ulid),
        "filter_signature": filter_sig,
        "fingerprint": fingerprint,
    }
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
    return p


def clear_base(local_sync_record_path: "str | Path") -> None:
    """Remove the sidecar. Absence is fine -- this is used on teardown paths."""
    base_path_for(local_sync_record_path).unlink(missing_ok=True)


def has_usable_base(
    local_sync_record_path: "str | Path",
    *,
    sync_record_ulid,
    filter_sig: str,
) -> bool:
    """
    Would `local_tree_differs` accept this baseline? -- WITHOUT walking the
    tree. Used by the convergence paths ("does this box still need a
    baseline?") and by doctor's coverage gauge, so the two can never disagree
    about what "usable" means.
    """
    base = read_base(local_sync_record_path)
    return (
        base is not None
        and base["sync_record_ulid"] == str(sync_record_ulid)
        and base.get("filter_signature") == filter_sig
    )


# %%
#|export
def local_tree_differs(
    *,
    local_path: "str | Path",
    local_sync_record_path: "str | Path",
    local_sync_record_ulid: "str | None",
    rclone_config_path: "str | Path | None" = None,
    exclude_file: "str | Path | None" = None,
    filter_sig: str,
) -> "bool | None":
    """
    Has this machine's tree changed since the sync the baseline describes?

    True / False when it can be answered from the baseline; **None means
    UNKNOWN** -- no baseline, or one bound to a different sync -- and the caller
    must decide what to do about that. None is deliberately not collapsed into
    True here: "unknown" is safe in the records-match branch (force a reconcile)
    and actively harmful in the remote-newer branch, where treating it as
    modified turns every pending pull into a CONFLICT. Only the caller knows
    which branch it is in.

    Unknown is also what a filter-signature change produces, which is what makes
    editing a box's exclude list reconcile the newly in-scope files.
    """
    base = read_base(local_sync_record_path)
    if base is None:
        return None
    if local_sync_record_ulid is None or base["sync_record_ulid"] != str(
        local_sync_record_ulid
    ):
        return None
    if base.get("filter_signature") != filter_sig:
        return None

    current = tree_fingerprint(
        local_path,
        rclone_config_path=rclone_config_path,
        exclude_file=exclude_file,
        filter_sig=filter_sig,
    )
    if current is None:
        # The tree is gone entirely. That is an existence question, not a
        # modification one, and the caller's existence logic already answers it.
        return None
    return current != base["fingerprint"]

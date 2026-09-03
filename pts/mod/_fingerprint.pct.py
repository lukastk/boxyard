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
# ## Non-literal filters OVER-detect, deliberately
#
# `literal_exclude_names` does not interpret glob patterns, so a glob-excluded
# file is invisible to the exclude set and therefore lands IN the digest. Its
# churn then reads as a change.
#
# That is the contract this codebase already had, not a new flaw: the same
# docstring says a glob-excluded file "can still make a box look modified (a
# false 'needs push', which sync then resolves as a no-op)", and there are tests
# for that path.
#
# The first version of this module REFUSED to fingerprint such a box, on the
# reasoning that an approximation would churn for ever. That was wrong twice
# over. It broke a supported feature -- the suite failed on the very tests that
# exist to prove glob-excluded files are handled -- and the churn argument does
# not hold: a false "changed" leads to a push (or, on a non-owner machine, the
# probe) which transfers nothing and then RE-RECORDS the baseline, so the next
# check compares against the churned tree and reads clean. It converges, and the
# thing that makes it converge is the baseline write that had to exist anyway.

# %%
#|default_exp _fingerprint

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import hashlib
import json
import os
import stat as stat_mod
from pathlib import Path

from boxyard import const


FINGERPRINT_VERSION = 1
"""Bumped when the digest's INPUTS change, so old sidecars read as unusable
rather than being compared against a digest computed a different way."""


# %%
#|export
def filter_signature(
    exclude_path: "str | Path | None",
    include_path: "str | Path | None" = None,
    filters_path: "str | Path | None" = None,
) -> str:
    """
    A digest of the ACTIVE filter rules, so changing scope forces a reconcile.

    Blank lines and whole-line comments are normalised away, so reformatting a
    comment does not churn every box. This is not a reimplementation of rclone's
    matching -- it only has to change when the rules change.
    """
    parts: list[str] = []
    for label, path in (
        ("exclude", exclude_path),
        ("include", include_path),
        ("filters", filters_path),
    ):
        if path is None:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        rules = [
            ln.strip()
            for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        parts.append(f"{label}:" + "\n".join(rules))
    digest = hashlib.sha256("\n--\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


# %%
#|export
def tree_fingerprint(
    path: "str | Path",
    exclude_names: "set[str] | None" = None,
    *,
    filter_sig: str = "",
) -> "str | None":
    """
    A digest of everything under `path` that the transport would compare.

    Returns None ONLY when the root does not exist -- "there is no tree here",
    which the caller's existence logic must answer, not this function. An
    existing but EMPTY tree digests to a real, stable value, so emptying a box
    is a change like any other and its deletions propagate.

    Per entry: the relative path, the kind, the size, the mtime in nanoseconds,
    and -- for regular files -- the owner-execute bit. For symlinks, the target
    string instead of a size, because the target IS what rclone writes into the
    `.rclonelink` file, making this the transport's own view rather than a guess
    about symlink semantics.

    Entries are hashed individually and the digests sorted before the final
    hash, so memory stays bounded on a box with hundreds of thousands of files
    rather than holding every path tuple at once.

    An unreadable directory RAISES, exactly as `check_last_time_modified` does
    and for the same reason: swallowing it would shrink the tree, and a smaller
    tree hashes to a digest that says "unchanged" about files it never looked
    at. A directory that vanishes mid-walk is a legitimate race and tolerated.
    """
    exclude_names = exclude_names or set()
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return None

    entry_digests: list[str] = []

    def record(rel: str, kind: str, size_or_target, mtime_ns: int, execbit: str) -> None:
        line = f"{rel}\0{kind}\0{size_or_target}\0{mtime_ns}\0{execbit}"
        entry_digests.append(hashlib.sha256(line.encode("utf-8", "surrogateescape")).hexdigest())

    if root.is_file():
        st = root.lstat()
        record(root.name, "file", st.st_size, st.st_mtime_ns,
               "x" if st.st_mode & stat_mod.S_IXUSR else "-")
    else:
        stack = [str(root)]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except FileNotFoundError:
                continue
            except OSError as e:
                raise OSError(
                    f"Cannot fingerprint '{root}': '{current}' could not be read "
                    f"({e}). Fix the permissions, or exclude it from the box."
                ) from e

            for entry in entries:
                if entry.name in exclude_names:
                    continue
                rel = Path(entry.path).relative_to(root).as_posix()
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat_mod.S_ISLNK(st.st_mode):
                    try:
                        target = os.readlink(entry.path)
                    except OSError:
                        continue
                    record(rel, "symlink", target, st.st_mtime_ns, "-")
                elif entry.is_dir(follow_symlinks=False):
                    # Directories are descended into but NOT recorded: rclone
                    # does not sync empty directories, and recording them would
                    # let an excluded file's arrival move a directory mtime and
                    # so change the digest -- the .DS_Store false positive this
                    # design exists to avoid.
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    record(rel, "file", st.st_size, st.st_mtime_ns,
                           "x" if st.st_mode & stat_mod.S_IXUSR else "-")
                # Anything else (fifo, socket, device) is skipped, matching what
                # rclone will transfer -- parity, not an omission.

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
    exclude_names: "set[str] | None",
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

    current = tree_fingerprint(local_path, exclude_names, filter_sig=filter_sig)
    if current is None:
        # The tree is gone entirely. That is an existence question, not a
        # modification one, and the caller's existence logic already answers it.
        return None
    return current != base["fingerprint"]

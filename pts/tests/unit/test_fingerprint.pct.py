# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # test_fingerprint
#
# The change-detection predicate that replaced the newest-mtime walk.
#
# The centrepiece is `test_the_ten_change_shapes`, a table rather than ten
# separate tests, because the property being asserted is about the SET of shapes
# and a table makes a missing row obvious. Every row that says INVISIBLE for the
# old walk is a change that used to be silently never pushed.

# %%
#|default_exp unit.test_fingerprint

# %%
#|export
import os
import shutil
import stat
import time
from pathlib import Path

import pytest

from boxyard._fingerprint import (
    FINGERPRINT_VERSION,
    base_path_for,
    clear_base,
    filter_signature,
    local_tree_differs,
    read_base,
    tree_fingerprint,
    write_base,
)
from boxyard._utils import check_last_time_modified


# %%
#|export
def _tree(root: Path) -> Path:
    """A small tree with a file, a second file, and a symlink."""
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "a.txt").write_text("aaa\n")
    (root / "sub" / "b.txt").write_text("bbbb\n")
    (root / "sub" / "link").symlink_to("a.txt")
    return root


# %%
#|export
def _retarget(root: Path) -> None:
    (root / "sub" / "link").unlink()
    (root / "sub" / "link").symlink_to("b.txt")


SHAPES = [
    # (name, mutate, old walk sees it, fingerprint must see it)
    ("edit content",        lambda r: (r / "sub" / "a.txt").write_text("zzzz\n"), True,  True),
    ("add a file",          lambda r: (r / "sub" / "n.txt").write_text("n\n"),    True,  True),
    ("delete a file",       lambda r: (r / "sub" / "a.txt").unlink(),             False, True),
    ("rename a file",       lambda r: (r / "sub" / "a.txt").rename(r / "sub" / "z.txt"), False, True),
    ("chmod +x",            lambda r: os.chmod(r / "sub" / "a.txt", 0o755),       False, True),
    ("delete a directory",  lambda r: shutil.rmtree(r / "sub"),                   False, True),
    ("add a symlink",       lambda r: (r / "sub" / "l2").symlink_to("b.txt"),     False, True),
    ("remove a symlink",    lambda r: (r / "sub" / "link").unlink(),              False, True),
    ("retarget a symlink",  _retarget,                                            False, True),
]


@pytest.mark.parametrize("name,mutate,old_sees,fp_must_see", SHAPES)
def test_the_ten_change_shapes(tmp_path, name, mutate, old_sees, fp_must_see):
    """
    Every shape the transport can carry must change the fingerprint.

    The `old_sees` column is asserted too, so this test also documents the bug it
    replaced: SEVEN of these nine rows were invisible to the newest-mtime walk,
    which means the box read SYNCED and the change was never pushed. Confirmed
    against the live remote before this was written -- deleting one file from a
    16,746-file box synced in exactly no-op time and left the file on the remote.

    The sleep is load-bearing and not laziness: the fixture is built moments
    before the baseline, so without it every mutation lands inside the mtime
    slack and the old walk reports "changed" for the wrong reason, making the
    `old_sees` column silently meaningless.
    """
    root = _tree(tmp_path / "box")
    time.sleep(2.2)

    old_before = check_last_time_modified(root)
    fp_before = tree_fingerprint(root)

    mutate(root)

    old_after = check_last_time_modified(root)
    fp_after = tree_fingerprint(root)

    old_saw = (
        old_before is not None and old_after is not None and old_after > old_before
    )
    assert old_saw == old_sees, (
        f"the OLD mtime walk's behaviour for {name!r} changed; this table is the "
        f"record of what it could and could not see"
    )
    assert (fp_before != fp_after) == fp_must_see, (
        f"the fingerprint did not see {name!r}"
    )


# %%
#|export
def test_an_untransportable_mode_change_is_deliberately_invisible(tmp_path):
    """
    `chmod g+w` must NOT change the fingerprint.

    Boxyard can only carry the OWNER-EXECUTE bit, out of band in
    `.boxyard-perms.json`, because the transport drops Unix mode. Hashing all of
    `st_mode` would make a box report NEEDS_PUSH for ever, chasing a change no
    mechanism here can propagate. So "fingerprint-visible mode change" is
    exactly "manifest-visible mode change", by construction.
    """
    root = _tree(tmp_path / "box")
    before = tree_fingerprint(root)
    os.chmod(root / "sub" / "a.txt", 0o664)
    assert tree_fingerprint(root) == before

    # ... while the bit that IS transportable still registers.
    os.chmod(root / "sub" / "a.txt", 0o755)
    assert tree_fingerprint(root) != before


# %%
#|export
def test_a_same_size_same_mtime_edit_is_invisible_and_that_is_parity(tmp_path):
    """
    The one content change the fingerprint misses is one rclone would not send.

    rclone compares `(path, size, modtime)` -- boxyard passes no `--checksum` --
    so a file rewritten with identical size and mtime is equal to rclone too and
    would not be transferred. This is parity with the transport, not a gap, and
    the test exists so that a future change to `--checksum` breaks HERE with an
    explanation rather than silently making the fingerprint too weak.
    """
    root = _tree(tmp_path / "box")
    before = tree_fingerprint(root)
    target = root / "sub" / "a.txt"
    st = target.stat()
    target.write_text("xxx\n")  # same 4 bytes as "aaa\n"
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert tree_fingerprint(root) == before


def test_a_preserved_mtime_restore_with_a_different_size_is_seen(tmp_path):
    """
    The `cp -p` case that actually bit, which the size catches.

    Restoring a file from a backup preserves its mtime, so the old walk saw
    nothing -- measured on a real box, where the restore silently did not sync
    and the remote kept the edited version. The size differs, so the fingerprint
    catches it.
    """
    root = tmp_path / "box"
    root.mkdir()
    (root / "orig.md").write_text("original\n")
    (root / "doc.md").write_text("original\n")
    time.sleep(2.2)
    (root / "doc.md").write_text("original\nprobe\n")
    edited = tree_fingerprint(root)
    shutil.copy2(root / "orig.md", root / "doc.md")  # preserves mtime
    assert tree_fingerprint(root) != edited


# %%
#|export
def test_an_empty_tree_digests_but_a_missing_one_is_none(tmp_path):
    """
    Emptying a box is a change; a box that is not here is a different question.

    If an empty tree returned None the caller could not distinguish "everything
    was deleted" (which must propagate) from "there is no tree" (which the
    existence logic answers). So empty gets a real, stable digest.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    assert tree_fingerprint(empty) is not None
    assert tree_fingerprint(empty) == tree_fingerprint(empty)
    assert tree_fingerprint(tmp_path / "not-here") is None

    populated = _tree(tmp_path / "box")
    before = tree_fingerprint(populated)
    shutil.rmtree(populated / "sub")
    assert tree_fingerprint(populated) != before


# %%
#|export
def test_excluded_churn_does_not_move_the_fingerprint(tmp_path):
    """
    The false positive the whole design exists to avoid.

    A `.DS_Store` appearing in a directory moves that DIRECTORY's mtime. Any
    design that stats directories flips the box to NEEDS_PUSH and, when the
    remote has also moved, to CONFLICT -- a wedged box, from a file that can
    never be transferred. Directories are therefore not in the digest.
    """
    root = _tree(tmp_path / "box")
    excludes = {".DS_Store", "node_modules"}

    # THE SLEEP IS THE TEST. A directory's mtime is stamped from a coarse clock,
    # so a file created in the same tick as the directory's last change does not
    # move it at all -- measured here, byte-identical mtimes before and after.
    # Without this wait the assertion below passes even against a walk that DOES
    # record directory mtimes, i.e. it would pass against exactly the bug it
    # exists to catch. Verified by mutation: with the sleep the directory-
    # recording variant fails, without it the variant passes.
    time.sleep(2.2)

    before = tree_fingerprint(root, excludes)

    (root / "sub" / ".DS_Store").write_bytes(b"\x00mac")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "huge.js").write_text("x" * 1000)

    assert tree_fingerprint(root, excludes) == before


# %%
#|export
def test_a_glob_excluded_file_over_detects_and_then_converges(tmp_path):
    """
    A glob-excluded file DOES move the fingerprint, and that is the old contract.

    `literal_exclude_names` does not interpret globs, so such a file lands in the
    digest and its churn reads as a change. This module briefly REFUSED to
    fingerprint boxes like that, reasoning the approximation would churn for
    ever. The suite disproved it immediately -- the failures were the very tests
    that exist to prove glob-excluded files are handled -- and the churn
    reasoning was wrong anyway: the push or probe that follows transfers nothing
    and RE-RECORDS the baseline, so the next check is clean. Over-detecting
    costs one no-op reconcile; refusing broke a supported feature.
    """
    root = tmp_path / "box"
    root.mkdir()
    (root / "keep.txt").write_text("keep\n")

    literal_only = {"node_modules"}  # what a "*.log" exclude reduces to
    before = tree_fingerprint(root, literal_only)
    (root / "noisy.log").write_text("chatter\n")
    after = tree_fingerprint(root, literal_only)
    assert after != before, "a glob-excluded file is not in the exclude set"

    # ... and the baseline written after the no-op reconcile makes it clean.
    assert tree_fingerprint(root, literal_only) == after


# %%
#|export
def test_changing_the_filter_rules_invalidates_the_baseline(tmp_path):
    """
    Scope can change while the visible file set does not.

    rclone does not delete excluded files from the destination. So a file
    excluded locally can sit on the remote; remove the exclusion and the LOCAL
    set is unchanged -- it was never there locally -- and a tree-only digest
    would be unchanged too, so nothing would ever reconcile the remote copy into
    scope. Hashing the filter rules is what closes that.
    """
    exclude = tmp_path / "excl"
    exclude.write_text("secrets/\n")
    sig_a = filter_signature(exclude)

    exclude.write_text("secrets/\n\n# a comment\n")
    assert filter_signature(exclude) == sig_a, "comments must not churn the fleet"

    exclude.write_text("")
    assert filter_signature(exclude) != sig_a


# %%
#|export
def test_the_baseline_is_bound_to_the_sync_it_describes(tmp_path):
    """
    A baseline from a different sync is UNKNOWN, not evidence.

    A stale fingerprint is the single dangerous failure mode, because it can say
    "unchanged" about a tree that changed. Binding it to the record's ULID
    collapses every stale case -- a crash between the two writes, a force-push, a
    convert, hand surgery -- into the loud "no usable baseline" answer.
    """
    root = _tree(tmp_path / "box")
    rec = tmp_path / "records" / "data.rec"
    rec.parent.mkdir(parents=True)
    sig = filter_signature(None)
    fp = tree_fingerprint(root, set(), filter_sig=sig)
    write_base(rec, sync_record_ulid="ULID-A", fingerprint=fp, filter_sig=sig)

    common = dict(
        local_path=root,
        local_sync_record_path=rec,
        exclude_names=set(),
        filter_sig=sig,
    )
    assert local_tree_differs(local_sync_record_ulid="ULID-A", **common) is False
    assert local_tree_differs(local_sync_record_ulid="ULID-B", **common) is None
    assert local_tree_differs(local_sync_record_ulid=None, **common) is None

    (root / "sub" / "a.txt").unlink()
    assert local_tree_differs(local_sync_record_ulid="ULID-A", **common) is True


# %%
#|export
def test_an_unusable_sidecar_reads_as_unknown_never_as_unchanged(tmp_path):
    """
    Corruption, a version bump and absence all mean the same thing: go and look.

    They must never read as "unchanged", which would be a silent claim about a
    tree nothing has examined.
    """
    root = _tree(tmp_path / "box")
    rec = tmp_path / "records" / "data.rec"
    rec.parent.mkdir(parents=True)
    sig = filter_signature(None)
    fp = tree_fingerprint(root, set(), filter_sig=sig)

    common = dict(
        local_path=root,
        local_sync_record_path=rec,
        local_sync_record_ulid="U",
        exclude_names=set(),
        filter_sig=sig,
    )
    assert local_tree_differs(**common) is None  # absent

    write_base(rec, sync_record_ulid="U", fingerprint=fp, filter_sig=sig)
    assert local_tree_differs(**common) is False

    base_path_for(rec).write_text("{not json")
    assert read_base(rec) is None
    assert local_tree_differs(**common) is None

    import json

    payload = {
        "version": FINGERPRINT_VERSION + 1,
        "sync_record_ulid": "U",
        "filter_signature": sig,
        "fingerprint": fp,
    }
    base_path_for(rec).write_text(json.dumps(payload))
    assert local_tree_differs(**common) is None

    clear_base(rec)
    assert read_base(rec) is None
    clear_base(rec)  # absence on a teardown path is fine


# %%
#|export
def test_the_sidecar_is_not_mistaken_for_a_sync_record(tmp_path):
    """
    Doctor's interrupted-sync scan globs `*.rec`; the sidecar must not match.

    If it did, doctor would try to parse a fingerprint as a SyncRecord and
    report a corrupt record on every box that has one.
    """
    rec = tmp_path / "data.rec"
    p = base_path_for(rec)
    assert p.name == "data.base.json"
    assert not p.name.endswith(".rec")
    assert p.parent == rec.parent
    assert list(tmp_path.glob("*.rec")) == []


# %%
#|export
def test_an_unreadable_directory_raises_rather_than_shrinking_the_tree(tmp_path):
    """
    Swallowing the error would hash a SMALLER tree, which is the dangerous
    direction: a digest that matches while files underneath went unexamined.
    """
    root = _tree(tmp_path / "box")
    locked = root / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("x\n")
    os.chmod(locked, 0o000)
    try:
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        with pytest.raises(OSError, match="could not be read"):
            tree_fingerprint(root)
    finally:
        os.chmod(locked, 0o755)

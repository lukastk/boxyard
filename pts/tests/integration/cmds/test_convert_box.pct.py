# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `boxyard convert` — the interruption table, proved
#
# Conversion is the first thing that writes to a remote, and a machine on an
# older boxyard cannot read a restic-backed box. The gate that makes that a loud
# refusal rather than corruption is the pair `boxes/<box>/data/` and
# `sync_records/<box>/data.rec` — and the ORDER decides whether a window exists.
#
# These tests are the table in the module docstring, one test per row: for each
# state a crash can leave, what this machine sees, what an un-upgraded peer
# does, and that the next run recovers. Two yards sharing one alias remote stand
# for two machines; the "un-upgraded" one is simply `sync_box`, which is the code
# an old machine runs.

# %%
#|default_exp integration.cmds.test_convert_box

# %%
#|export
import asyncio
import shutil
from pathlib import Path

import pytest

from boxyard import const
from boxyard._enums import BoxPart, StorageFormat
from boxyard._models import BoxMeta, get_boxyard_meta
from boxyard.cmds import (
    convert_box,
    include_box,
    new_box,
    sync_box,
    sync_missing_boxmetas,
)
from boxyard.cmds._convert_box import ConversionRefused, compare_trees
from boxyard.config import get_config
from tests.integration.conftest import create_boxyards

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("restic") is None, reason="restic binary not available"
    ),
]

PASSWORD = "convert-test-password"


def run(coro):
    return asyncio.run(coro)


def tree(path: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(path)): p.read_bytes()
        for p in sorted(Path(path).rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


# %%
#|export
@pytest.fixture
def two(monkeypatch, tmp_path):
    """
    Machine A (converts) and machine B (stands for an un-upgraded peer), sharing
    one remote. B holds a real checkout, which is what makes its refusals
    meaningful.
    """
    monkeypatch.setenv("BOXYARD_RESTIC_PASSWORD", PASSWORD)
    monkeypatch.setattr(
        "boxyard.const.RESTIC_CANONICAL_ROOT", str(tmp_path / "canon")
    )
    monkeypatch.setattr(
        "boxyard._restic.const.RESTIC_CANONICAL_ROOT", str(tmp_path / "canon")
    )

    remote_name, remote_root, yards = create_boxyards(num_boxyards=2)
    (cfgA, cpA, _dpA), (cfgB, cpB, _dpB) = yards

    idx = new_box(config_path=cpA, box_name="convbox",
                  storage_location=remote_name, claim=False)
    dataA = get_boxyard_meta(cfgA).by_index_name[idx].get_local_part_path(
        cfgA, BoxPart.DATA
    )
    (dataA / "notes.md").write_text("first\n")
    (dataA / "sub").mkdir(exist_ok=True)
    (dataA / "sub" / "keep.txt").write_text("keep\n")
    (dataA / "run.sh").write_text("#!/bin/sh\n")
    (dataA / "run.sh").chmod(0o755)
    run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))

    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=idx, read_only=True))
    run(sync_box(config_path=cpB, box_index_name=idx, verbose=False))
    dataB = get_boxyard_meta(cfgB).by_index_name[idx].get_local_part_path(
        cfgB, BoxPart.DATA
    )

    box_root = remote_root / "boxyard" / const.REMOTE_BOXES_REL_PATH / idx
    return {
        "idx": idx, "cfgA": cfgA, "cpA": cpA, "cfgB": cfgB, "cpB": cpB,
        "dataA": dataA, "dataB": dataB, "remote_root": remote_root,
        "box_root": box_root,
        "rec": remote_root / "boxyard" / const.SYNC_RECORDS_REL_PATH / idx
               / f"{BoxPart.DATA.value}.rec",
    }


def peer_sync(two):
    """What an un-upgraded machine does. Returns ('ok', conditions) or ('refused', msg)."""
    try:
        results = run(sync_box(config_path=two["cpB"], box_index_name=two["idx"],
                               verbose=False))
        return "ok", {k.value: v[0].sync_condition.value for k, v in results.items()}
    except Exception as exc:
        return "refused", str(exc)


def fmt(two, which="cfgA"):
    cfg = two[which]
    return BoxMeta.load(cfg, "my_remote", two["idx"]).storage_format


# %% [markdown]
# ## Row 0 — before anything: a plain box that syncs

# %%
#|export
def test_row0_untouched_box_syncs_on_both(two):
    assert (two["box_root"] / const.BOX_DATA_REL_PATH).is_dir()
    assert two["rec"].exists()
    status, detail = peer_sync(two)
    assert status == "ok", detail
    assert fmt(two) is StorageFormat.PLAIN


# %% [markdown]
# ## Rows 1-2 — the repository exists, nothing is removed yet
#
# The whole of the push and the verification happen before a single byte of the
# plain box is touched. An old machine cannot even see the repository.

# %%
#|export
def test_rows1_2_a_dry_run_changes_nothing(two):
    result = run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                             dry_run=True, verbose=False))
    assert result["dry_run"] is True
    assert result["steps"] == []
    assert not (two["box_root"] / const.BOX_RESTIC_REL_PATH).exists()
    assert (two["box_root"] / const.BOX_DATA_REL_PATH).is_dir()
    assert two["rec"].exists()
    assert fmt(two) is StorageFormat.PLAIN
    status, detail = peer_sync(two)
    assert status == "ok", detail


def test_a_dry_run_reports_the_local_shape(two):
    result = run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                             dry_run=True, verbose=False))
    assert result["local_files"] >= 3
    assert result["local_bytes"] > 0


def test_a_dry_run_can_measure_what_restic_would_store(two):
    """
    Measured against a LOCAL throwaway repo, so a dry run writes nothing to the
    remote. `data_added_packed` is what would actually land there.
    """
    result = run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                             dry_run=True, estimate_size=True, verbose=False))
    assert result["estimated_stored_bytes"] > 0
    assert not (two["box_root"] / const.BOX_RESTIC_REL_PATH).exists()


# %% [markdown]
# ## Row 6 — the complete conversion

# %%
#|export
def test_a_full_conversion_leaves_the_expected_remote(two):
    result = run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                             verbose=False))
    assert result["snapshot_id"]
    assert (two["box_root"] / const.BOX_RESTIC_REL_PATH).is_dir()
    assert (two["box_root"] / const.BOX_SNAPSHOT_POINTER_REL_PATH).is_file()
    assert not (two["box_root"] / const.BOX_DATA_REL_PATH).exists()
    assert not two["rec"].exists()
    assert fmt(two) is StorageFormat.RESTIC


def test_a_file_written_during_the_conversion_is_not_declared_synced(two, monkeypatch):
    """
    `synced_at_unix` must predate the snapshot, not the end of the conversion.

    The unit test in `test_change_detection` pins the MECHANISM -- that a late
    stamp hides a mid-flight write. This one pins THE CALL SITE, which that test
    cannot: removing `now_unix=` from `convert_box` leaves the mechanism test
    passing and the bug back. So this asserts the argument actually arrives, and
    that the value predates the push finishing.

    The defect this guards was measured on a live box worked in during its
    conversion: git wrote objects at 14:09:07, the stamp landed at 14:12:24, and
    the next sync took exactly no-op time with 81 files missing from the
    snapshot -- invisible for ever, because the snapshot predated the files and
    the timestamp postdated them.
    """
    import time

    import boxyard.cmds._convert_box as mod

    seen = {}
    real = mod.write_state

    def spy(*a, **kw):
        seen.update(kw)
        seen["positional"] = a
        return real(*a, **kw)

    monkeypatch.setattr(mod, "write_state", spy)

    before = time.time()
    result = run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                             verbose=False))
    after = time.time()
    assert result["snapshot_id"]

    assert "now_unix" in seen, (
        "convert_box called write_state without now_unix, so the state records "
        "the moment the conversion ENDED -- anything written while the push was "
        "reading the tree becomes permanently invisible"
    )
    stamp = seen["now_unix"]
    assert before <= stamp <= after, f"stamp {stamp} outside the conversion window"
    # It must be nearer the START than the end: the push is the slow part, and
    # the whole point is that the stamp predates it.
    assert stamp - before < (after - before) / 2 + 1.0, (
        "the stamp looks like it was taken after the push rather than before it"
    )


def test_the_local_data_is_untouched_by_conversion(two):
    """Conversion changes where the box is STORED, never the box."""
    before = tree(two["dataA"])
    run(convert_box(config_path=two["cpA"], box_index_name=two["idx"], verbose=False))
    assert tree(two["dataA"]) == before
    assert (two["dataA"] / "run.sh").stat().st_mode & 0o777 == 0o755


def test_an_upgraded_machine_can_restore_the_converted_box(two, tmp_path):
    from boxyard._restic import (
        ResticRepo, pull, rclone_program_for, repo_url_for_box,
        resolve_restic_password,
    )

    result = run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                             verbose=False))
    cfg = two["cfgA"]
    repo = ResticRepo(
        url=repo_url_for_box(cfg.storage_locations["my_remote"].store_path,
                             "my_remote", two["idx"]),
        password=resolve_restic_password(cfg),
        cache_dir=tmp_path / "cache2",
        rclone_program=rclone_program_for(cfg.rclone_config_path),
    )
    (tmp_path / "cache2").mkdir(exist_ok=True)
    dest = tmp_path / "restored"
    run(pull(repo, dest, target_snapshot=result["snapshot_id"], base_snapshot=None, excludes=[]))
    # Compared the way the conversion itself does: the exec-bit manifest is
    # excluded from the snapshot on purpose, because restic carries mode
    # natively, so its absence from the restore is the design working.
    assert compare_trees(two["dataA"], dest, []) == []
    assert (dest / "run.sh").stat().st_mode & 0o777 == 0o755
    assert not (dest / const.BOX_PERMS_MANIFEST_REL_PATH).exists()


def test_an_upgraded_peer_adopts_a_converted_box(two):
    """
    The peer here runs THIS build -- `peer_sync` calls the real `sync_box` --
    so what it proves is that an UPGRADED machine holding a plain checkout
    converges onto the converted box without anyone intervening.

    This test used to assert the opposite, and passed for a bad reason. Before
    conversion published the boxmeta, the remote said `plain` while its data was
    restic, so this peer read the box as plain, went looking for a `data/` that
    had just been purged, and raised. The refusal was the DEFECT, not the
    design, and calling it "an un-upgraded peer refuses" hid it: nothing in this
    file ever simulated an un-upgraded build. That is
    `test_stale_machine_meets_restic.py`, which really does.
    """
    run(convert_box(config_path=two["cpA"], box_index_name=two["idx"], verbose=False))
    before = tree(two["dataB"])

    status, detail = peer_sync(two)
    assert status == "ok", detail
    assert detail["data"] == "needs_pull", (
        f"the peer should have adopted the converted box, got {detail}"
    )

    assert fmt(two, "cfgB") is StorageFormat.RESTIC, (
        "conversion must publish the format, or every other machine reads the "
        "box as plain and looks for a data/ that is gone"
    )
    # The exec-bit manifest is the one thing that legitimately goes: it is
    # excluded from every snapshot on purpose (restic carries mode natively),
    # and `restore --delete` therefore removes the peer's stale copy. The
    # machine that CONVERTED keeps its own, because it never restores -- a
    # harmless asymmetry, and the reason it is named here rather than folded
    # into the comparison silently.
    _manifest = const.BOX_PERMS_MANIFEST_REL_PATH
    assert not (two["dataB"] / _manifest).exists(), (
        "the peer kept a manifest that is not in the snapshot"
    )
    assert (two["dataB"] / "run.sh").stat().st_mode & 0o777 == 0o755, (
        "the exec bit survived WITHOUT the manifest, which is the point"
    )
    assert {k: v for k, v in tree(two["dataB"]).items() if k != _manifest} == {
        k: v for k, v in before.items() if k != _manifest
    }, "the peer's content changed while adopting an unmodified copy"
    assert not (two["box_root"] / const.BOX_DATA_REL_PATH).exists(), (
        "the peer resurrected the plain tree"
    )


# %% [markdown]
# ## Rows 3-5 — the interrupted states
#
# Each is produced by stopping after the real step, then asked the same two
# questions: does an un-upgraded peer do damage, and does a re-run recover?

# %%
#|export
def convert_upto(two, stop_after):
    """Run the conversion but stop after a named step, as a crash would."""
    import boxyard.cmds._convert_box as mod

    # The absent-ok variants, which is what `convert_box` calls: a resume
    # re-runs from the top and legitimately finds these already done.
    from boxyard._utils import rclone_delete_absent_ok, rclone_purge_absent_ok

    real_delete, real_purge = rclone_delete_absent_ok, rclone_purge_absent_ok
    state = {"deleted": False, "purged": False}

    async def fake_delete(**kw):
        await real_delete(**kw)
        state["deleted"] = True
        if stop_after == "rec":
            raise KeyboardInterrupt("simulated crash after the sync record")
        return True

    async def fake_purge(**kw):
        await real_purge(**kw)
        state["purged"] = True
        if stop_after == "data":
            raise KeyboardInterrupt("simulated crash after the plain tree")
        return True

    async def fake_pointer(*a, **kw):
        from boxyard._restic import write_pointer as real
        await real(*a, **kw)
        if stop_after == "pointer":
            raise KeyboardInterrupt("simulated crash after the pointer")

    mod.rclone_delete_absent_ok = fake_delete
    mod.rclone_purge_absent_ok = fake_purge
    if stop_after == "pointer":
        mod.write_pointer = fake_pointer
    try:
        with pytest.raises(KeyboardInterrupt):
            run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                            verbose=False))
    finally:
        mod.rclone_delete_absent_ok = real_delete
        mod.rclone_purge_absent_ok = real_purge
        from boxyard._restic import write_pointer as real_pointer
        mod.write_pointer = real_pointer
    return state


def test_row3_crash_after_the_sync_record(two):
    """
    `data/` still present, `.rec` gone. This is the state the ORDER exists to
    create: `get_sync_status` refuses immediately on
    "remote path exists, but remote sync record does not exist".
    """
    convert_upto(two, "rec")
    assert (two["box_root"] / const.BOX_DATA_REL_PATH).is_dir()
    assert not two["rec"].exists()

    before = tree(two["dataB"])
    status, detail = peer_sync(two)
    assert status == "refused", f"expected a refusal, got {detail}"
    assert tree(two["dataB"]) == before

    run(convert_box(config_path=two["cpA"], box_index_name=two["idx"], verbose=False))
    assert fmt(two) is StorageFormat.RESTIC
    assert not (two["box_root"] / const.BOX_DATA_REL_PATH).exists()


def test_row4_crash_after_the_plain_tree(two):
    convert_upto(two, "data")
    assert not (two["box_root"] / const.BOX_DATA_REL_PATH).exists()
    assert not two["rec"].exists()
    assert not (two["box_root"] / const.BOX_SNAPSHOT_POINTER_REL_PATH).exists()

    before = tree(two["dataB"])
    status, detail = peer_sync(two)
    assert status == "refused", f"expected a refusal, got {detail}"
    assert tree(two["dataB"]) == before

    run(convert_box(config_path=two["cpA"], box_index_name=two["idx"], verbose=False))
    assert fmt(two) is StorageFormat.RESTIC


def test_row5_crash_after_the_pointer(two):
    convert_upto(two, "pointer")
    assert (two["box_root"] / const.BOX_SNAPSHOT_POINTER_REL_PATH).is_file()
    assert fmt(two) is StorageFormat.PLAIN, "boxmeta not yet updated"

    before = tree(two["dataB"])
    status, detail = peer_sync(two)
    assert status == "refused", f"expected a refusal, got {detail}"
    assert tree(two["dataB"]) == before

    run(convert_box(config_path=two["cpA"], box_index_name=two["idx"], verbose=False))
    assert fmt(two) is StorageFormat.RESTIC


def test_a_rerun_after_a_completed_conversion_is_a_no_op(two):
    run(convert_box(config_path=two["cpA"], box_index_name=two["idx"], verbose=False))
    again = run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                            verbose=False))
    assert again["already"] == "converted"
    assert again["steps"] == []


# %% [markdown]
# ## Verification happens before anything is removed

# %%
#|export
def test_a_failed_verification_removes_nothing(two, monkeypatch):
    """
    The load-bearing promise. If the restore does not match, the box must be
    exactly as it was -- plain tree, sync record, boxmeta, all intact.
    """
    import boxyard.cmds._convert_box as mod

    monkeypatch.setattr(mod, "compare_trees",
                        lambda *a, **k: ["content differs: notes.md"])

    with pytest.raises(ConversionRefused) as excinfo:
        run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                        verbose=False))
    assert "NOT identical" in str(excinfo.value)

    assert (two["box_root"] / const.BOX_DATA_REL_PATH).is_dir()
    assert two["rec"].exists()
    assert fmt(two) is StorageFormat.PLAIN
    status, detail = peer_sync(two)
    assert status == "ok", detail


def test_compare_trees_notices_content_mode_and_symlinks(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "sub").mkdir(parents=True)
        (root / "f.txt").write_text("same\n")
        (root / "sub" / "g.txt").write_text("same\n")
        (root / "link").symlink_to("f.txt")
    assert compare_trees(a, b, []) == []

    (b / "f.txt").write_text("different\n")
    assert any("content differs" in p or "size differs" in p for p in compare_trees(a, b, []))

    (b / "f.txt").write_text("same\n")
    (b / "sub" / "g.txt").chmod(0o700)
    assert any("mode differs" in p for p in compare_trees(a, b, []))

    (b / "sub" / "g.txt").chmod((a / "sub" / "g.txt").stat().st_mode & 0o777)
    (b / "link").unlink()
    (b / "link").symlink_to("sub/g.txt")
    assert any("symlink target differs" in p for p in compare_trees(a, b, []))


def test_compare_trees_notices_a_missing_or_extra_path(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "only-in-a.txt").write_text("x")
    assert any("missing from the restore" in p for p in compare_trees(a, b, []))
    (b / "only-in-a.txt").write_text("x")
    (b / "extra.txt").write_text("y")
    assert any("present only in the restore" in p for p in compare_trees(a, b, []))


def test_compare_trees_ignores_the_excluded_perms_manifest(tmp_path):
    """It is excluded from the snapshot on purpose, so it is not a difference."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("same\n")
    (b / "f.txt").write_text("same\n")
    (a / const.BOX_PERMS_MANIFEST_REL_PATH).write_text("{}")
    assert compare_trees(a, b, []) == []


# %%
#|export
def test_compare_trees_honours_the_push_excludes(tmp_path):
    """
    An excluded path is NOT a difference -- the push never stored it.

    THE BUG THIS PINS. `compare_trees` used to compare the whole local tree
    against a snapshot the push had correctly filtered, so every `.venv/` and
    `__pycache__/` in the box read as "missing from the restore". The first real
    box converted with it -- 28,060 files -- refused with 16,201 differences, all
    of them excluded paths, and the conversion was in fact perfect.

    Every other test in this file passed the whole time, because each one builds
    a tree with no excluded content in it. So this test builds one WITH.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    (a / ".venv" / "lib").mkdir(parents=True)
    (a / "src").mkdir(parents=True)
    (b / "src").mkdir(parents=True)
    (a / ".venv" / "lib" / "big.so").write_text("x" * 100)
    (a / "src" / "__pycache__").mkdir()
    (a / "src" / "__pycache__" / "m.pyc").write_text("bytecode")
    (a / "src" / "m.py").write_text("real\n")
    (b / "src" / "m.py").write_text("real\n")

    excludes = [".venv", "__pycache__"]
    assert compare_trees(a, b, excludes) == []

    # ... and without them, the same identical pair is a pile of false failures.
    assert len(compare_trees(a, b, [])) >= 4


# %%
#|export
def test_compare_trees_still_reports_an_excluded_path_the_restore_invented(tmp_path):
    """
    Pruning is one-sided on purpose.

    If restic ever DOES carry an excluded path, that is a real defect and must
    stay visible. Filtering both sides would hide it.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    (b / ".venv").mkdir(parents=True)
    (b / ".venv" / "leaked.so").write_text("should not be here")

    problems = compare_trees(a, b, [".venv"])
    assert any("present only in the restore" in x for x in problems)


# %% [markdown]
# ## Refusals

# %%
#|export
def test_a_box_being_synced_right_now_is_refused(two):
    """
    Detected by taking the SAME per-box lock `sync_box` holds for the whole of a
    sync, non-blocking. Catches a supervisor pass mid-flight.
    """
    from boxyard._utils.locking import BoxyardLockManager

    manager = BoxyardLockManager(two["cfgA"].boxyard_data_path)
    path = manager.box_sync_lock_path(two["idx"])
    manager._ensure_lock_dir(path)
    held = __import__("filelock").FileLock(path, timeout=0)
    held.acquire()
    try:
        with pytest.raises(ConversionRefused) as excinfo:
            run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                            verbose=False))
        assert "synced right now" in str(excinfo.value)
    finally:
        held.release()
    assert (two["box_root"] / const.BOX_DATA_REL_PATH).is_dir()


def test_an_interrupted_sync_is_refused(two):
    """
    The other half: no process holds the lock, but the box's contents are not
    settled, so "byte-identical" would be verifying a torn tree.
    """
    from boxyard._models import SyncRecord

    cfg = two["cfgA"]
    meta = BoxMeta.load(cfg, "my_remote", two["idx"])
    rec_path = meta.get_local_sync_record_path(cfg, BoxPart.DATA)
    incomplete = SyncRecord.create(sync_complete=False)
    Path(rec_path).write_text(incomplete.model_dump_json())

    with pytest.raises(ConversionRefused) as excinfo:
        run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                        verbose=False))
    assert "interrupted" in str(excinfo.value)
    assert (two["box_root"] / const.BOX_DATA_REL_PATH).is_dir()


def test_a_box_not_checked_out_here_is_refused(two):
    """
    Verification compares the restore against the local tree, so a machine
    without one cannot verify and must not convert.
    """
    shutil.rmtree(two["dataA"])
    with pytest.raises(ConversionRefused) as excinfo:
        run(convert_box(config_path=two["cpA"], box_index_name=two["idx"],
                        verbose=False))
    assert "not checked out" in str(excinfo.value)
    assert (two["box_root"] / const.BOX_DATA_REL_PATH).is_dir()


def test_an_unknown_box_is_refused(two):
    with pytest.raises(ValueError):
        run(convert_box(config_path=two["cpA"],
                        box_index_name="20260101_zzzzzz__nope", verbose=False))

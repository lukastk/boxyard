# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `boxyard convert --to-plain`
#
# Conversion was the one act in boxyard with no way out, and the state with no
# exit was the FINAL one — the worst place to put it. The safety case for the
# whole design is that every state is recoverable, which is what the forward
# interruption table demonstrates; this closes the gap.
#
# ## The step order is the OPPOSITE of the forward one, and that is the point
#
# Forward, the switch is enforced by DESTROYING the old format's sync record
# first, because a machine on an older boxyard cannot read `storage_format` at
# all — publishing the boxmeta would redirect nobody, so the only way to stop
# those machines acting on the plain tree is to make every read of it an error.
#
# Reverse has the opposite property: `plain` is the format EVERY build
# understands, including one that has never heard of the key (an absent
# `storage_format` reads as plain). So the switch is a PUBLISH, performed at the
# moment both formats are complete, and the old one is removed only afterwards.
#
# Removing the repository or pointer while the boxmeta still says `restic`
# reaches the state "declares restic, has neither repo nor pointer", which
# `sync_data_restic` reads as a box that has never been pushed — the next
# machine to sync would INITIALISE A NEW REPOSITORY and push, recreating the
# format this command is leaving. Publishing first makes that unreachable.
#
# ## The interruption table
#
# | # | after | remote holds | this machine | a peer | recovery |
# |---|---|---|---|---|---|
# | 0 | nothing | repo, pointer, boxmeta=restic | restic, syncs | restic, syncs | start again |
# | 1 | verified | unchanged | restic, syncs | restic, syncs | as 0 |
# | 2 | plain tree pushed | + `data/`, `data.rec` | restic, syncs | restic, syncs | re-push is a no-op |
# | 3 | boxmeta PUBLISHED | both formats, boxmeta=plain | plain, syncs | plain, syncs | re-run cleans up |
# | 4 | pointer deleted | repo remains | plain, syncs | plain, syncs | re-run cleans up |
# | 5 | repo purged | `data/`, `data.rec`, boxmeta=plain | plain, syncs | plain, syncs | done |
#
# **Every row is a WORKING state, not a refusal** — a stronger property than the
# forward direction has, and a direct consequence of the ordering: at every
# point the boxmeta names a format that is complete on the remote.

# %%
#|default_exp integration.cmds.test_convert_to_plain

# %%
#|export
import asyncio
import hashlib
import os
import shutil
import stat
from pathlib import Path

import pytest

from boxyard import const
from boxyard._enums import BoxPart, StorageFormat
from boxyard._models import BoxMeta, SyncCondition, get_boxyard_meta
from boxyard._restic import read_state
from boxyard.cmds import (
    convert_box,
    include_box,
    new_box,
    sync_box,
    sync_missing_boxmetas,
)
from boxyard.cmds._convert_box import ReversalRefused
from boxyard.config import get_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("restic") is None, reason="restic binary not available"
    ),
]


def run(coro):
    return asyncio.run(coro)


def fingerprint(root: Path) -> dict:
    """{relpath: (sha256 or link target, mode)} — content, mode AND symlinks."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        rel = str(p.relative_to(root))
        if rel == const.BOX_PERMS_MANIFEST_REL_PATH:
            # Generated for a plain box and excluded from every snapshot, so it
            # legitimately differs across a round trip. Asserted separately.
            continue
        if {".venv", "__pycache__"} & set(rel.split("/")):
            # Excluded from the box, so it is on this machine and never on the
            # remote or in a snapshot. Its PRESENCE is asserted separately: the
            # reversal must leave it alone rather than delete it.
            continue
        if rel == ".git" or rel.startswith(".git/"):
            # `.git` is in `_PRUNED_DIR_NAMES`, so the exec-bit manifest
            # deliberately never covers it -- a pre-existing plain-path
            # trade-off, taken to stop the manifest listing thousands of
            # entries. The visible consequence is that `.git/hooks/*.sample`
            # arrive at 644 on any machine that adopts a PLAIN box, converted or
            # not: verified against a box that was never converted at all, so it
            # is not something this command introduced. Comparing it here would
            # be asserting that pre-existing gap rather than the round trip.
            continue
        if p.is_symlink():
            out[rel] = (f"link:{os.readlink(p)}", "link")
        elif p.is_file():
            out[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(),
                        oct(stat.S_IMODE(p.lstat().st_mode)))
        elif p.is_dir():
            out[rel] = ("dir", oct(stat.S_IMODE(p.lstat().st_mode)))
    return out


def data_of(cfg, idx) -> Path:
    return get_boxyard_meta(cfg).by_index_name[idx].get_local_part_path(cfg, BoxPart.DATA)


def fmt(config_path, remote_name, idx) -> StorageFormat:
    return BoxMeta.load(get_config(config_path), remote_name, idx).storage_format


# %%
#|export
@pytest.fixture
def converted(monkeypatch, tmp_path):
    """A two-machine yard with a box already converted to restic on A."""
    from tests.integration.conftest import create_boxyards

    monkeypatch.setenv("BOXYARD_RESTIC_PASSWORD", "to-plain-test")
    for target in ("boxyard.const", "boxyard._restic.const"):
        monkeypatch.setattr(f"{target}.RESTIC_CANONICAL_ROOT", str(tmp_path / "canon"))

    remote_name, remote_root, yards = create_boxyards(num_boxyards=2)
    (cfgA, cpA, _), (cfgB, cpB, _) = yards

    idx = new_box(config_path=cpA, box_name="roundtrip",
                  storage_location=remote_name, claim=False)
    d = data_of(get_config(cpA), idx)
    (d / "src" / "nested").mkdir(parents=True)
    (d / "README.md").write_text("readme\n")
    (d / "src" / "a.txt").write_text("aaa\n")
    (d / "src" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (d / "src" / "run.sh").chmod(0o755)
    (d / "src" / "nested" / "keep.txt").write_text("keep\n")
    (d / "src" / "link-to-readme").symlink_to("../README.md")
    (d / "big.bin").write_bytes(bytes(range(256)) * 200)

    # EXCLUDED content, and the fixture is wrong without it.
    #
    # The forward conversion shipped comparing the whole local checkout against
    # a snapshot written THROUGH the exclude list, so every excluded path came
    # back as a difference -- 16,201 of them on a real box, all `.venv/` and
    # `__pycache__/`. It could not have succeeded on any box holding a
    # virtualenv. It reached a real machine because no test fixture contained
    # anything excluded, so the round trip never exercised the exclude list at
    # all.
    (d / ".venv" / "lib").mkdir(parents=True)
    (d / ".venv" / "lib" / "thing.so").write_text("a built artefact\n")
    (d / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")
    (d / "src" / "__pycache__").mkdir()
    (d / "src" / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\x00compiled")

    run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))
    original = fingerprint(d)
    run(convert_box(config_path=cpA, box_index_name=idx, verbose=False))

    return {
        "idx": idx, "cpA": cpA, "cpB": cpB, "dataA": d,
        "remote_name": remote_name, "remote_root": remote_root,
        "original": original,
        "box_root": remote_root / "boxyard" / const.REMOTE_BOXES_REL_PATH / idx,
    }


# %% [markdown]
# ## The round trip

# %%
#|export
def test_plain_to_restic_to_plain_is_byte_identical(converted):
    """
    The whole point: content, MODE and SYMLINK TARGETS all survive both
    directions, compared against the tree as it was before any conversion.
    """
    run(convert_box(config_path=converted["cpA"], box_index_name=converted["idx"],
                    to_plain=True, verbose=False))

    d = converted["dataA"]
    assert fingerprint(d) == converted["original"], (
        "the round trip did not return the box to what it was"
    )
    assert stat.S_IMODE((d / "src" / "run.sh").lstat().st_mode) == 0o755
    assert (d / "src" / "link-to-readme").is_symlink()
    assert os.readlink(d / "src" / "link-to-readme") == "../README.md"


def test_the_remote_is_a_plain_tree_again(converted):
    run(convert_box(config_path=converted["cpA"], box_index_name=converted["idx"],
                    to_plain=True, verbose=False))

    box_root = converted["box_root"]
    assert (box_root / const.BOX_DATA_REL_PATH).is_dir(), "no plain data/"
    assert not (box_root / const.BOX_RESTIC_REL_PATH).exists(), "repository left behind"
    assert not (box_root / const.BOX_SNAPSHOT_POINTER_REL_PATH).exists(), (
        "data.snapshot left behind"
    )
    assert fmt(converted["cpA"], converted["remote_name"],
               converted["idx"]) is StorageFormat.PLAIN


def test_the_format_is_PUBLISHED_not_just_saved_locally(converted):
    """
    Defect 1 in the forward direction was a boxmeta saved locally and never
    pushed, so the fleet read the wrong format. It must not come back backwards.
    """
    run(convert_box(config_path=converted["cpA"], box_index_name=converted["idx"],
                    to_plain=True, verbose=False))

    cpB = converted["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    assert fmt(cpB, converted["remote_name"],
               converted["idx"]) is StorageFormat.PLAIN, (
        "the remote's boxmeta still says restic -- the fleet would break"
    )


def test_the_box_still_syncs_afterwards(converted):
    run(convert_box(config_path=converted["cpA"], box_index_name=converted["idx"],
                    to_plain=True, verbose=False))
    results = run(sync_box(config_path=converted["cpA"],
                           box_index_name=converted["idx"], verbose=False))
    assert results[BoxPart.DATA][0].sync_condition is SyncCondition.SYNCED

    assert read_state(get_config(converted["cpA"]).boxyard_data_path,
                      converted["idx"]) is None, (
        "this machine's restic state describes a repository that is gone"
    )


def test_a_second_machine_can_adopt_the_reverted_box(converted):
    """The reverted box must be usable from elsewhere, not just locally."""
    run(convert_box(config_path=converted["cpA"], box_index_name=converted["idx"],
                    to_plain=True, verbose=False))
    cpB = converted["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=converted["idx"]))

    dataB = data_of(get_config(cpB), converted["idx"])
    assert fingerprint(dataB) == converted["original"]


# %% [markdown]
# ## The interruption table, row by row
#
# Each state is produced by stopping after the real step, then asked the same
# three questions: what does THIS machine do, what does a PEER do, and does a
# re-run recover?
#
# The forward table's rows are refusals. These are not, and that is the
# ordering working: at every point the boxmeta names a format that is complete
# on the remote, so there is nothing to refuse.

# %%
#|export
def reverse_upto(converted, stop_after):
    """Run the reversal but stop after a named step, as a crash would."""
    import boxyard.cmds._convert_box as mod

    real_sync_helper = mod.sync_helper
    real_delete = mod.rclone_delete_absent_ok
    real_purge = mod.rclone_purge_absent_ok
    real_save = BoxMeta.save

    async def fake_sync_helper(*a, **kw):
        out = await real_sync_helper(*a, **kw)
        if stop_after == "pushed":
            raise KeyboardInterrupt("simulated crash after the plain tree")
        return out

    def fake_save(self, *a, **kw):
        out = real_save(self, *a, **kw)
        if stop_after == "published" and self.storage_format is StorageFormat.PLAIN:
            raise KeyboardInterrupt("simulated crash at the publish")
        return out

    async def fake_delete(*a, **kw):
        out = await real_delete(*a, **kw)
        if stop_after == "pointer":
            raise KeyboardInterrupt("simulated crash after the pointer")
        return out

    async def fake_purge(*a, **kw):
        out = await real_purge(*a, **kw)
        if stop_after == "repo":
            raise KeyboardInterrupt("simulated crash after the repo")
        return out

    mod.sync_helper = fake_sync_helper
    mod.rclone_delete_absent_ok = fake_delete
    mod.rclone_purge_absent_ok = fake_purge
    if stop_after == "published":
        BoxMeta.save = fake_save
    try:
        with pytest.raises(KeyboardInterrupt):
            run(convert_box(config_path=converted["cpA"],
                            box_index_name=converted["idx"],
                            to_plain=True, verbose=False))
    finally:
        mod.sync_helper = real_sync_helper
        mod.rclone_delete_absent_ok = real_delete
        mod.rclone_purge_absent_ok = real_purge
        BoxMeta.save = real_save


def peer_sync(converted):
    """What the OTHER machine does. ('ok', conditions) or ('refused', message)."""
    cpB = converted["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    try:
        results = run(sync_box(config_path=cpB, box_index_name=converted["idx"],
                               verbose=False))
        return "ok", {k.value: v[0].sync_condition.value for k, v in results.items()}
    except Exception as exc:
        return "refused", str(exc)


@pytest.mark.parametrize("stop_after", ["pushed", "published", "pointer"])
def test_every_interrupted_state_still_works_and_recovers(converted, stop_after):
    """
    Row 2, 3 and 4. Each must leave a WORKING box on both machines, and a re-run
    must complete the reversal and leave the content byte-identical.
    """
    reverse_upto(converted, stop_after)

    status, detail = peer_sync(converted)
    assert status == "ok", f"row '{stop_after}' left the peer refusing: {detail}"

    run(convert_box(config_path=converted["cpA"], box_index_name=converted["idx"],
                    to_plain=True, verbose=False))

    assert fingerprint(converted["dataA"]) == converted["original"]
    assert fmt(converted["cpA"], converted["remote_name"],
               converted["idx"]) is StorageFormat.PLAIN
    box_root = converted["box_root"]
    assert not (box_root / const.BOX_RESTIC_REL_PATH).exists()
    assert not (box_root / const.BOX_SNAPSHOT_POINTER_REL_PATH).exists()


def test_the_publish_happens_BEFORE_the_repository_is_removed(converted):
    """
    THE ordering property, asserted directly rather than inferred.

    Stopped right after the publish, the remote must still hold the repository
    AND the pointer. If they went first, the box would sit declaring restic with
    neither -- which `sync_data_restic` reads as never-pushed, and the next
    machine to sync would create a NEW repository and push into it, recreating
    the format this command exists to leave.
    """
    reverse_upto(converted, "published")

    box_root = converted["box_root"]
    assert (box_root / const.BOX_RESTIC_REL_PATH).exists(), (
        "the repository was removed before the format was published"
    )
    assert (box_root / const.BOX_SNAPSHOT_POINTER_REL_PATH).exists()
    assert (box_root / const.BOX_DATA_REL_PATH).is_dir(), (
        "and the plain tree it redirects everyone onto must already be there"
    )


def test_a_peer_that_still_reads_restic_does_not_lose_the_repository(converted):
    """
    Row 2: the plain tree is pushed but the format is not published yet, so the
    fleet is still on restic -- and restic must still be entirely intact.
    """
    reverse_upto(converted, "pushed")

    run(sync_missing_boxmetas(config_path=converted["cpB"], verbose=False))
    assert fmt(converted["cpB"], converted["remote_name"],
               converted["idx"]) is StorageFormat.RESTIC
    box_root = converted["box_root"]
    assert (box_root / const.BOX_RESTIC_REL_PATH).exists()
    assert (box_root / const.BOX_SNAPSHOT_POINTER_REL_PATH).exists()

    status, detail = peer_sync(converted)
    assert status == "ok", detail


# %% [markdown]
# ## Refusals, and a dry run that changes nothing

# %%
#|export
def test_dry_run_changes_nothing(converted):
    box_root = converted["box_root"]
    before = fingerprint(converted["dataA"])

    result = run(convert_box(config_path=converted["cpA"],
                             box_index_name=converted["idx"],
                             to_plain=True, dry_run=True, verbose=False))

    assert result["direction"] == "to-plain"
    assert (box_root / const.BOX_RESTIC_REL_PATH).exists(), "dry run removed the repo"
    assert (box_root / const.BOX_SNAPSHOT_POINTER_REL_PATH).exists()
    assert not (box_root / const.BOX_DATA_REL_PATH).exists(), (
        "dry run pushed a plain tree"
    )
    assert fmt(converted["cpA"], converted["remote_name"],
               converted["idx"]) is StorageFormat.RESTIC
    assert fingerprint(converted["dataA"]) == before


def test_it_refuses_when_restic_is_missing(converted, monkeypatch):
    """
    Reversing READS the repository to verify the local tree, so it needs the
    binary. Refuse before writing or printing anything.
    """
    from boxyard import const as _const
    from boxyard._restic import ResticError

    monkeypatch.setattr("boxyard._restic._restic_binary", None)
    monkeypatch.setenv(_const.ENV_VAR_BOXYARD_RESTIC, "/nonexistent/restic")

    with pytest.raises(ResticError) as excinfo:
        run(convert_box(config_path=converted["cpA"],
                        box_index_name=converted["idx"],
                        to_plain=True, verbose=False))
    assert "reverse" in str(excinfo.value)
    assert (converted["box_root"] / const.BOX_RESTIC_REL_PATH).exists()


def test_it_refuses_while_the_box_is_being_synced(converted):
    """The same lock `sync_box` holds, taken non-blocking."""
    from boxyard._utils.locking import BoxyardLockManager
    from boxyard.cmds._convert_box import ConversionRefused

    config = get_config(converted["cpA"])
    manager = BoxyardLockManager(config.boxyard_data_path)
    lock_path = manager.box_sync_lock_path(converted["idx"])
    manager._ensure_lock_dir(lock_path)
    held = __import__("filelock").FileLock(lock_path, timeout=0)
    held.acquire()
    try:
        with pytest.raises(ConversionRefused) as excinfo:
            run(convert_box(config_path=converted["cpA"],
                            box_index_name=converted["idx"],
                            to_plain=True, verbose=False))
        assert "being synced right now" in str(excinfo.value)
    finally:
        held.release()


def test_it_refuses_a_local_tree_that_does_not_match_the_snapshot(converted):
    """
    Verification happens BEFORE anything is destroyed. A local edit that was
    never pushed must stop the reversal with the repository still intact.
    """
    (converted["dataA"] / "README.md").write_text("an unpushed local edit\n")

    with pytest.raises(ReversalRefused) as excinfo:
        run(convert_box(config_path=converted["cpA"],
                        box_index_name=converted["idx"],
                        to_plain=True, verbose=False))

    assert "NOT identical" in str(excinfo.value)
    box_root = converted["box_root"]
    assert (box_root / const.BOX_RESTIC_REL_PATH).exists(), "the repo was removed"
    assert fmt(converted["cpA"], converted["remote_name"],
               converted["idx"]) is StorageFormat.RESTIC


def test_reversing_a_plain_box_is_a_no_op(converted):
    run(convert_box(config_path=converted["cpA"], box_index_name=converted["idx"],
                    to_plain=True, verbose=False))
    result = run(convert_box(config_path=converted["cpA"],
                             box_index_name=converted["idx"],
                             to_plain=True, verbose=False))
    assert result["already"] == "plain"


# %% [markdown]
# ## Excluded content
#
# The defect that reached a real box, in both directions: the snapshot is
# written THROUGH the exclude list, so comparing the whole local checkout
# against it reports every excluded path as a difference. It survived because
# no fixture held anything excluded — so the round trip never exercised the
# exclude list at all.

# %%
#|export
def test_a_box_with_a_virtualenv_can_be_reversed(converted):
    """
    The regression. Without `excludes` in the comparison this raises
    `ReversalRefused` listing every `.venv/` and `__pycache__/` path.
    """
    run(convert_box(config_path=converted["cpA"], box_index_name=converted["idx"],
                    to_plain=True, verbose=False))
    assert fmt(converted["cpA"], converted["remote_name"],
               converted["idx"]) is StorageFormat.PLAIN


def test_the_reversal_leaves_excluded_content_alone(converted):
    """
    Excluded paths live only on this machine. The reversal must neither send
    them to the remote nor delete them locally.
    """
    run(convert_box(config_path=converted["cpA"], box_index_name=converted["idx"],
                    to_plain=True, verbose=False))

    d = converted["dataA"]
    assert (d / ".venv" / "lib" / "thing.so").read_text() == "a built artefact\n"
    assert (d / "src" / "__pycache__" / "a.cpython-311.pyc").exists()

    remote_data = converted["box_root"] / const.BOX_DATA_REL_PATH
    assert not (remote_data / ".venv").exists(), (
        "an excluded directory was pushed to the remote"
    )
    assert not (remote_data / "src" / "__pycache__").exists()

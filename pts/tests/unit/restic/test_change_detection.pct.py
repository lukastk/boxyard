# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # What `local_is_modified` can and cannot see
#
# The audit this file exists to hold down. Three change-detection defects were
# found by RUNNING boxyard on real machines and none by the suite: deletions,
# adoption, and then symlinks. Each was patched individually. The common cause
# was the same each time — the predicate counted restic's **node kinds**
# (`files_new`, `files_changed`, `dirs_new`) and anything that is not a file or
# a directory moved none of them.
#
# So this is not a test for symlinks. It is one case per thing a box can
# contain and per thing that can change about it, and it fails if ANY of them
# stops being seen. The design note carries the same table with the measured
# restic counters beside it.
#
# What the predicate asks now is `tree_blobs > 1`. A directory's tree object is
# a Merkle node over every child's name, type, mode, size, content hash and
# symlink target, so restic writes a new one exactly when something changes,
# plus one per ancestor. A no-op adds exactly one — the snapshot's own root.

# %%
#|default_exp unit.restic.test_change_detection

# %%
#|export
import asyncio
import os
import shutil
import time
from pathlib import Path

import pytest

from boxyard._restic import (
    ResticRepo,
    init_repo,
    local_is_modified,
    push,
)

pytestmark = pytest.mark.skipif(
    shutil.which("restic") is None and not os.environ.get("BOXYARD_RESTIC"),
    reason="restic binary not available",
)


def run(coro):
    return asyncio.run(coro)


# %%
#|export
@pytest.fixture
def box(tmp_path):
    """
    A repo plus a pushed tree holding one of everything: a regular file, a
    nested directory, an executable, an EMPTY directory and an EXISTING symlink
    (so retargeting and deleting one can be tested, not only adding).

    Returns (repo, data_path, push_result) with the tree already pushed, so
    every test starts from a box that is genuinely settled.
    """
    repo = ResticRepo(
        url=str(tmp_path / "repo"),
        password="detection-test",
        cache_dir=tmp_path / "cache",
    )
    (tmp_path / "repo").mkdir()
    (tmp_path / "cache").mkdir()
    run(init_repo(repo))

    d = tmp_path / "boxroot" / "data"
    (d / "src" / "nested").mkdir(parents=True)
    (d / "README.md").write_text("readme\n")
    (d / "src" / "a.txt").write_text("aaa\n")
    (d / "src" / "run.sh").write_text("#!/bin/sh\n")
    (d / "src" / "run.sh").chmod(0o755)
    (d / "src" / "nested" / "keep.txt").write_text("keep\n")
    (d / "emptydir").mkdir()
    (d / "existing-link").symlink_to("README.md")

    result = run(push(repo, d, parent=None, box_index_name=None))
    return repo, d, result


def modified(box) -> bool:
    repo, d, result = box
    return run(
        local_is_modified(
            repo, d, result.snapshot_id, expected_files=result.files
        )
    )


# %% [markdown]
# ## The control: a settled box must stay settled
#
# Without this the whole file could pass by always answering "changed".

# %%
#|export
def test_an_unchanged_box_is_not_modified(box):
    assert modified(box) is False


def test_the_answer_is_stable_when_asked_twice(box):
    assert modified(box) is False
    assert modified(box) is False


# %% [markdown]
# ## Regular files

# %%
#|export
def test_adding_a_file_is_seen(box):
    (box[1] / "new.txt").write_text("new\n")
    assert modified(box) is True


def test_adding_an_EMPTY_file_is_seen(box):
    (box[1] / "zero.txt").write_text("")
    assert modified(box) is True


def test_editing_content_is_seen(box):
    (box[1] / "README.md").write_text("readme CHANGED\n")
    assert modified(box) is True


def test_deleting_a_file_is_seen(box):
    """The FIRST defect found by running it: `backup --dry-run` walks the tree,
    so a removed file is simply not seen by the node-kind counters."""
    (box[1] / "src" / "a.txt").unlink()
    assert modified(box) is True


def test_renaming_a_file_is_seen(box):
    (box[1] / "README.md").rename(box[1] / "READYOU.md")
    assert modified(box) is True


def test_adding_a_nested_file_is_seen(box):
    (box[1] / "src" / "nested" / "extra.txt").write_text("x\n")
    assert modified(box) is True


# %% [markdown]
# ## Symlinks — the THIRD defect, and it was four cases, not one
#
# A symlink is neither a file nor a directory, so every one of these moved
# `files_new`, `files_changed` and `dirs_new` by exactly zero. Adding one was
# what got reported; retargeting and deleting were missed too and were found by
# building the table rather than by fixing the report.

# %%
#|export
def test_adding_a_symlink_to_a_file_is_seen(box):
    (box[1] / "link-to-keep").symlink_to("src/nested/keep.txt")
    assert modified(box) is True


def test_adding_a_symlink_to_a_directory_is_seen(box):
    (box[1] / "alias-src").symlink_to("src")
    assert modified(box) is True


def test_RETARGETING_a_symlink_in_place_is_seen(box):
    """Same name, different target: nothing is added or removed at all."""
    link = box[1] / "existing-link"
    link.unlink()
    link.symlink_to("src/a.txt")
    assert modified(box) is True


def test_deleting_a_symlink_is_seen(box):
    (box[1] / "existing-link").unlink()
    assert modified(box) is True


def test_replacing_a_file_with_a_symlink_of_the_same_name_is_seen(box):
    (box[1] / "README.md").unlink()
    (box[1] / "README.md").symlink_to("src/a.txt")
    assert modified(box) is True


# %% [markdown]
# ## Directories, including empty ones
#
# An empty directory holds no files, so a box whose only change is losing one
# moves no file counter.

# %%
#|export
def test_adding_an_empty_directory_is_seen(box):
    (box[1] / "brand-new-dir").mkdir()
    assert modified(box) is True


def test_deleting_an_empty_directory_is_seen(box):
    (box[1] / "emptydir").rmdir()
    assert modified(box) is True


def test_renaming_a_directory_is_seen(box):
    (box[1] / "src" / "nested").rename(box[1] / "src" / "renested")
    assert modified(box) is True


# %% [markdown]
# ## Modes — including the exec bit
#
# This one is worth stating plainly. The exec-bit manifest exists because
# rclone loses `+x` over backends that drop Unix mode, and the design retires it
# for restic boxes because restic carries mode natively. That is true of STORAGE
# and was NOT true of detection: a `chmod +x` and nothing else moved no counter,
# so the box would have reported SYNCED for ever and the bit would never have
# reached the remote at all.

# %%
#|export
def test_chmod_is_seen(box):
    (box[1] / "src" / "a.txt").chmod(0o600)
    assert modified(box) is True


def test_setting_the_exec_bit_and_nothing_else_is_seen(box):
    (box[1] / "README.md").chmod(0o755)
    assert modified(box) is True


def test_clearing_the_exec_bit_and_nothing_else_is_seen(box):
    (box[1] / "src" / "run.sh").chmod(0o644)
    assert modified(box) is True


# %% [markdown]
# ## Failing towards "changed"
#
# The cost of a false "changed" is one unnecessary sync. The cost of a false
# "unchanged" is work that never leaves the machine, silently, for ever. They
# are not comparable, so anything unreadable must answer "changed". Pinned by
# `test_no_summary_line_at_all_reports_changed` below.

# %% [markdown]
# ## The false positives, which are as important as the misses
#
# A predicate that answers "changed" to everything sees all six missed shapes
# and is useless: every box would push on every pass, and a replica that cannot
# push would sit permanently dirty. Two candidate designs were written and
# thrown away for failing exactly here, so each is pinned.

# %%
#|export
def test_activity_in_the_boxs_PARENT_directory_is_not_a_change(box):
    """
    `tree_blobs` was measured as a perfect discriminator -- exactly 1 for a
    no-op at every depth and tree size, 4 or more for every row in the table
    above -- and was WRONG: a snapshot records the whole path chain, so an
    unrelated file in the box's parent moves it 1 -> 3. The canonical root lives
    under `/tmp`, which changes constantly.
    """
    repo, d, result = box
    (d.parent / "an-unrelated-sibling.txt").write_text("nothing to do with it\n")
    assert modified(box) is False


def test_touching_an_EXCLUDED_file_is_not_a_change(box):
    """
    The second design thrown away. Restic never reports a symlink as an item at
    all -- the only signal for one is the CONTAINING DIRECTORY being `modified`
    -- and an excluded file's touch bumps that same directory's mtime, so the
    two are indistinguishable in the item stream.

    This is not hypothetical: `.DS_Store` is excluded and Finder writes one into
    every directory it is shown, so on both Macs the box would have pushed on
    every pass, for ever.
    """
    repo, d, result = box
    excluded = d / "ignored.tmp"
    excluded.write_text("first\n")
    patterns = [str(d / "ignored.tmp")]
    # Push once with the file present and excluded, so it is settled.
    settled = run(push(repo, d, parent=None, excludes=patterns, box_index_name=None))
    assert run(local_is_modified(repo, d, settled.snapshot_id,
                                 excludes=patterns,
                                 expected_files=settled.files)) is False

    time.sleep(1.1)
    excluded.write_text("changed, and still excluded\n")
    assert run(local_is_modified(repo, d, settled.snapshot_id,
                                 excludes=patterns,
                                 expected_files=settled.files)) is False, (
        "an excluded file's write was read as a change to the box"
    )


def test_a_directorys_mtime_alone_is_not_a_change(box):
    """
    A restore does not reproduce the target directory's own mtime, so every
    machine that ADOPTS a box has different directory mtimes from the machine
    that pushed it. Counting those would leave every replica permanently dirty
    -- and a read-only replica could never clear it.
    """
    repo, d, result = box
    os.utime(d / "src", (10**9, 10**9))
    os.utime(d, (10**9, 10**9))
    assert modified(box) is False


# %% [markdown]
# ## When the answer cannot be known
#
# The cost of a false "changed" is one unnecessary sync. The cost of a false
# "unchanged" is work that never leaves the machine, silently, for ever. They
# are not comparable, so every unreadable case resolves towards "changed".

# %%
#|export
def test_a_forgotten_parent_snapshot_reports_changed(box, monkeypatch):
    """Retention removed the snapshot this machine recorded: nothing is known."""
    import boxyard._restic as restic_mod

    async def no_nodes(repo, snapshot):
        return None

    monkeypatch.setattr(restic_mod, "snapshot_nodes", no_nodes)
    assert modified(box) is True


def test_a_missing_summary_reports_changed(box, monkeypatch):
    """restic exited 0 without saying what it did."""
    import boxyard._restic as restic_mod

    real = restic_mod.run_restic

    async def no_summary(repo, args, **kwargs):
        if args and args[0] == "backup":
            return 0, "", ""
        return await real(repo, args, **kwargs)

    monkeypatch.setattr(restic_mod, "run_restic", no_summary)
    assert modified(box) is True


def test_a_failed_dry_run_reports_changed(box, monkeypatch):
    import boxyard._restic as restic_mod

    real = restic_mod.run_restic

    async def fails(repo, args, **kwargs):
        if args and args[0] == "backup":
            return 1, "", "boom"
        return await real(repo, args, **kwargs)

    monkeypatch.setattr(restic_mod, "run_restic", fails)
    assert modified(box) is True


def test_the_file_count_backstop_catches_a_deletion(box, monkeypatch):
    """
    Exercised rather than asserted: the count is compared against what the push
    recorded, so a file disappearing is caught even before the node comparison
    runs.
    """
    repo, d, result = box
    (d / "src" / "a.txt").unlink()
    assert run(
        local_is_modified(repo, d, result.snapshot_id, expected_files=result.files)
    ) is True

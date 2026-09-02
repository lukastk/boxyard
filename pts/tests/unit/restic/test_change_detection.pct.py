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
import stat
import time
from pathlib import Path

import pytest

from boxyard._restic import (
    GATE_SLACK_SECONDS,
    ResticRepo,
    tree_touched_since,
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


# %% [markdown]
# ## The cheap gate
#
# Parity with the plain backend, which has always short-circuited on a local
# walk and never contacted the remote when nothing was touched. Measured on
# mymain against the live remote: a no-op DATA sync of a 1,419,522-file plain
# box takes 8 SECONDS, while a 42-file restic box took 11 s, because restic paid
# ~2.2 s for `restic snapshots` regardless of size.
#
# THE POLARITY IS THE CONTRACT: the gate may only ever skip the expensive check
# when nothing at all has been touched. A false "maybe" costs a slow check; a
# false "no" loses an edit, silently, for ever. Every test below is written from
# that asymmetry.

# %%
#|export
def count_restic_calls(monkeypatch):
    """Record every restic invocation the next call makes."""
    import boxyard._restic as restic_mod

    seen = []
    real = restic_mod.run_restic

    async def counting(repo, args, **kwargs):
        seen.append(args[0] if args else "?")
        return await real(repo, args, **kwargs)

    monkeypatch.setattr(restic_mod, "run_restic", counting)
    return seen


def gated(box, *, synced_at, exclude_names=None):
    repo, d, result = box
    return run(
        local_is_modified(
            repo, d, result.snapshot_id,
            synced_at_unix=synced_at,
            expected_files=result.files,
            exclude_names=exclude_names,
        )
    )


def test_an_untouched_box_makes_NO_restic_calls_at_all(box, monkeypatch):
    """
    The whole point. Without this the box pays ~2.2 s of `restic snapshots`
    against the real remote on every pass, for ever, however small it is.
    """
    seen = count_restic_calls(monkeypatch)
    assert gated(box, synced_at=time.time() + 60) is False
    assert seen == [], f"the gate let {seen} through on an untouched box"


def test_the_gate_does_not_fire_without_a_recorded_time(box, monkeypatch):
    """
    No `synced_at_unix` means no baseline to compare against, so there is
    nothing the gate can prove and the full check must run.
    """
    repo, d, result = box
    # Past the slack first, so the tree is genuinely "old" and a gate that
    # wrongly invented a baseline WOULD short-circuit here. Without this sleep
    # the fresh fixture looks touched and the test passes without testing
    # anything -- mutation testing found exactly that.
    time.sleep(GATE_SLACK_SECONDS + 0.3)
    seen = count_restic_calls(monkeypatch)
    run(local_is_modified(repo, d, result.snapshot_id,
                          expected_files=result.files))
    assert seen, "the gate skipped the check with no recorded timestamp"


# %% [markdown]
# ### Polarity: every shape must still be seen THROUGH the gate
#
# One case per row of the audit table, with the gate active. If any of these
# returns False the gate has lost an edit.

# %%
#|export
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: (d / "new.txt").write_text("n\n"), id="add-file"),
        pytest.param(lambda d: (d / "README.md").write_text("CHANGED\n"), id="edit"),
        pytest.param(lambda d: (d / "src" / "a.txt").unlink(), id="delete-file"),
        pytest.param(lambda d: (d / "README.md").rename(d / "R2.md"), id="rename"),
        pytest.param(lambda d: (d / "l").symlink_to("README.md"), id="add-symlink"),
        pytest.param(lambda d: (d / "ad").symlink_to("src"), id="add-symlink-dir"),
        pytest.param(
            lambda d: ((d / "existing-link").unlink(),
                       (d / "existing-link").symlink_to("src/a.txt")),
            id="retarget-symlink",
        ),
        pytest.param(lambda d: (d / "existing-link").unlink(), id="delete-symlink"),
        pytest.param(lambda d: (d / "emptydir").rmdir(), id="delete-empty-dir"),
        pytest.param(lambda d: (d / "newdir").mkdir(), id="add-empty-dir"),
        pytest.param(lambda d: (d / "README.md").chmod(0o755), id="chmod-set-exec"),
        pytest.param(lambda d: (d / "src" / "run.sh").chmod(0o644), id="chmod-clear-exec"),
    ],
)
def test_the_gate_never_hides_a_change(box, mutate):
    """
    The gate is a SUFFICIENT condition, never a substitute for the check. Every
    shape in the audit table must survive it -- including both directions of
    `chmod`, which the plain backend's mtime-only walk cannot see.
    """
    repo, d, result = box
    # Past the slack BEFORE the baseline, or the freshly-built fixture is itself
    # within it, the gate says "touched" whatever the mutation was, and the test
    # passes without ever exercising the gate. Mutation testing caught exactly
    # that: an mtime-only gate survived this suite until this sleep was added.
    time.sleep(GATE_SLACK_SECONDS + 0.3)
    baseline = time.time()
    time.sleep(1.05)
    mutate(d)
    assert gated(box, synced_at=baseline) is True


def test_ctime_is_what_makes_it_sound(box):
    """
    Stated on its own because it is the trap. `chmod` moves ctime and leaves
    mtime alone, so the plain backend's `check_last_time_modified` -- which this
    deliberately does NOT reuse -- would gate a mode change away and reintroduce
    the defect one level up.
    """
    from boxyard._utils import check_last_time_modified

    repo, d, result = box
    time.sleep(GATE_SLACK_SECONDS + 0.3)  # see the note above
    baseline = time.time()
    time.sleep(1.05)
    (d / "README.md").chmod(0o755)

    assert check_last_time_modified(d).timestamp() <= baseline, (
        "precondition: the mtime-only walk cannot see a chmod"
    )
    assert tree_touched_since(d, baseline) is True, (
        "max(mtime, ctime) must see it"
    )


# %% [markdown]
# ### Being wrong towards "maybe" is free; being wrong towards "no" is not

# %%
#|export
def test_an_excluded_write_costs_a_check_and_answers_correctly(box, monkeypatch):
    """
    An excluded file's write bumps its directory's mtime, so the gate says
    "maybe" and the full check runs -- and correctly reports the box unchanged.
    Slower, never wrong. This is why the gate does least on actively-worked
    boxes, which is a cost rather than a correctness problem.
    """
    repo, d, result = box
    excluded = d / "ignored.tmp"
    excluded.write_text("first\n")
    patterns = [str(excluded)]
    settled = run(push(repo, d, parent=None, excludes=patterns, box_index_name=None))
    time.sleep(GATE_SLACK_SECONDS + 0.3)  # see the note about the slack below
    baseline = time.time()
    time.sleep(1.05)
    excluded.write_text("changed, still excluded\n")

    seen = count_restic_calls(monkeypatch)
    answer = run(local_is_modified(
        repo, d, settled.snapshot_id, synced_at_unix=baseline,
        excludes=patterns, expected_files=settled.files,
    ))
    assert answer is False, "an excluded write was reported as a change"
    assert seen, "precondition: the gate should NOT have short-circuited here"


def test_naming_an_excluded_DIRECTORY_lets_the_gate_fire(box, monkeypatch):
    """
    Where the exclude names DO help: a write deep inside `.venv/` bumps mtimes
    inside `.venv/`, and a walk that never descends into it never sees them.
    This is the case that matters in practice -- `.venv/` and `node_modules/`
    are where the churn is.
    """
    repo, d, result = box
    venv = d / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "thing.so").write_text("binary\n")
    patterns = [str(d / ".venv")]
    settled = run(push(repo, d, parent=None, excludes=patterns, box_index_name=None))
    # Past GATE_SLACK_SECONDS before taking the baseline: the tree was BUILT
    # moments ago, and the slack deliberately treats anything within two seconds
    # of the recorded time as possibly-later. Sleeping here tests the gate
    # rather than the slack.
    time.sleep(GATE_SLACK_SECONDS + 0.3)
    baseline = time.time()
    time.sleep(1.05)
    (venv / "thing.so").write_text("rebuilt\n")

    seen = count_restic_calls(monkeypatch)
    answer = run(local_is_modified(
        repo, d, settled.snapshot_id, synced_at_unix=baseline,
        excludes=patterns, expected_files=settled.files,
        exclude_names={".venv"},
    ))
    assert answer is False
    assert seen == [], "a write inside an excluded directory was not gated out"


def test_rewriting_a_named_excluded_file_is_gated_out(box, monkeypatch):
    """
    Rewriting an existing file changes ITS mtime and nothing else -- a
    directory's mtime moves only when an entry is created or removed. So a named
    exclude gates rewrites out completely.
    """
    repo, d, result = box
    excluded = d / "ignored.tmp"
    excluded.write_text("first\n")
    patterns = [str(excluded)]
    settled = run(push(repo, d, parent=None, excludes=patterns, box_index_name=None))
    time.sleep(GATE_SLACK_SECONDS + 0.3)
    baseline = time.time()
    time.sleep(1.05)
    excluded.write_text("changed, still excluded\n")

    seen = count_restic_calls(monkeypatch)
    answer = run(local_is_modified(
        repo, d, settled.snapshot_id, synced_at_unix=baseline,
        excludes=patterns, expected_files=settled.files,
        exclude_names={"ignored.tmp"},
    ))
    assert answer is False
    assert seen == [], "a rewrite of a named exclude was not gated out"


def test_CREATING_an_excluded_file_still_costs_a_check(box, monkeypatch):
    """
    The boundary, and the honest limit of the gate. Creating an entry bumps the
    holding directory's mtime, and that mtime cannot be ignored -- it is exactly
    what a DELETION relies on. So the gate says "maybe" and the full check runs
    and correctly reports the box unchanged.

    `.DS_Store` is this case the first time Finder displays a directory: on the
    Macs that pass pays the full check. Slower, never wrong.
    """
    repo, d, result = box
    patterns = [str(d / "ignored.tmp")]
    settled = run(push(repo, d, parent=None, excludes=patterns, box_index_name=None))
    time.sleep(GATE_SLACK_SECONDS + 0.3)
    baseline = time.time()
    time.sleep(1.05)
    (d / "ignored.tmp").write_text("brand new, and excluded\n")

    seen = count_restic_calls(monkeypatch)
    answer = run(local_is_modified(
        repo, d, settled.snapshot_id, synced_at_unix=baseline,
        excludes=patterns, expected_files=settled.files,
        exclude_names={"ignored.tmp"},
    ))
    assert answer is False, "and it must still answer correctly"
    assert seen, "expected the gate to say 'maybe' and the full check to run"


def test_an_unreadable_directory_raises_rather_than_gating_it_away(box):
    """
    Swallowing the error would LOWER the answer and gate out real changes
    underneath it -- the same failure the plain walk documents a scar for.
    """
    repo, d, result = box
    locked = d / "src" / "nested"
    original = stat.S_IMODE(locked.lstat().st_mode)
    locked.chmod(0o000)
    try:
        with pytest.raises(OSError):
            tree_touched_since(d, time.time() + 60)
    finally:
        locked.chmod(original)


# %%
#|export
def test_the_slack_covers_a_coarse_filesystem_timestamp(box):
    """
    Models the hazard `GATE_SLACK_SECONDS` exists for, which no filesystem here
    can reproduce: ext4 stores nanoseconds, but HFS+ stores whole SECONDS, so a
    write that genuinely happened after the recorded moment can be stamped
    slightly before it.

    Simulated by recording a time slightly LATER than the file's stamp -- which
    is exactly what a one-second-granularity filesystem produces. Without the
    slack this edit is gated away and lost silently; with it the box pays one
    unnecessary check.
    """
    repo, d, result = box
    edited = d / "README.md"
    edited.write_text("an edit whose stamp rounds down\n")
    stamped = edited.lstat().st_mtime

    # The recorded moment is 1 s AFTER the file's stamp, as coarse granularity
    # would leave it. A naive `stamp > recorded` reads that as "not touched".
    recorded = stamped + 1.0
    assert stamped <= recorded, "precondition: the stamp rounds below the record"

    assert tree_touched_since(d, recorded) is True, (
        "an edit was gated away because its timestamp rounded down -- this is "
        "what GATE_SLACK_SECONDS prevents"
    )

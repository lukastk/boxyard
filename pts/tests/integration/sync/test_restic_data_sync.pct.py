# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Syncing a restic-backed box through `sync_box`
#
# The first step that changes how a box syncs, so these tests are about the
# CONTRACT rather than the mechanism: the same `(SyncStatus, bool)` tuple, the
# same `SyncCondition` vocabulary, the same "raise when a person must decide,
# return a status when they must not", and the `multi-sync` board rendering the
# result without a special case.
#
# `test_a_plain_box_takes_the_untouched_path` is the one that matters most: the
# overwhelming majority of boxes stay plain for a long time.

# %%
#|default_exp integration.sync.test_restic_data_sync

# %%
#|export
import asyncio
import shutil
from pathlib import Path

import pytest

from boxyard import const
from boxyard._enums import BoxPart, StorageFormat, SyncDirection, SyncSetting
from boxyard._models import BoxMeta, SyncCondition, get_boxyard_meta
from boxyard._restic import read_state, write_state
from boxyard._utils.sync_helper import SyncUnsafe
from boxyard.cmds import (
    convert_box,
    include_box,
    new_box,
    sync_box,
    sync_missing_boxmetas,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("restic") is None, reason="restic binary not available"
    ),
]


def run(coro):
    return asyncio.run(coro)


def tree(path: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(path)): p.read_bytes()
        for p in sorted(Path(path).rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def data_of(cfg, idx) -> Path:
    return get_boxyard_meta(cfg).by_index_name[idx].get_local_part_path(cfg, BoxPart.DATA)


# %%
#|export
@pytest.fixture
def pair(monkeypatch, tmp_path):
    """Two machines, one remote, one CONVERTED box that both hold."""
    from tests.integration.conftest import create_boxyards

    monkeypatch.setenv("BOXYARD_RESTIC_PASSWORD", "sync-test-password")
    for target in ("boxyard.const", "boxyard._restic.const"):
        monkeypatch.setattr(f"{target}.RESTIC_CANONICAL_ROOT", str(tmp_path / "canon"))

    remote_name, remote_root, yards = create_boxyards(num_boxyards=2)
    (cfgA, cpA, _), (cfgB, cpB, _) = yards

    idx = new_box(config_path=cpA, box_name="rbox",
                  storage_location=remote_name, claim=False)
    dataA = data_of(cfgA, idx)
    (dataA / "notes.md").write_text("first\n")
    (dataA / "sub").mkdir(exist_ok=True)
    (dataA / "sub" / "keep.txt").write_text("keep\n")
    run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))

    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=idx, read_only=True))
    run(sync_box(config_path=cpB, box_index_name=idx, verbose=False))

    run(convert_box(config_path=cpA, box_index_name=idx, verbose=False))
    # Conversion changes the boxmeta LOCALLY; it does not push it. That is
    # deliberate -- conversion should not decide when the fleet learns -- so the
    # documented next step is a META sync, and B learns the ordinary way.
    run(sync_box(config_path=cpA, box_index_name=idx,
                 sync_choices=[BoxPart.META], verbose=False))
    run(sync_box(config_path=cpB, box_index_name=idx,
                 sync_choices=[BoxPart.META], verbose=False))

    return {
        "idx": idx, "cfgA": cfgA, "cpA": cpA, "cfgB": cfgB, "cpB": cpB,
        "dataA": dataA, "dataB": data_of(cfgB, idx),
        "remote_root": remote_root, "remote_name": remote_name,
    }


def sync(pair, machine="A", **kwargs):
    return run(sync_box(config_path=pair[f"cp{machine}"],
                        box_index_name=pair["idx"], verbose=False, **kwargs))


def data_condition(results):
    return results[BoxPart.DATA][0].sync_condition


# %% [markdown]
# ## The plain path is untouched
#
# The thing a regression here would cost is much larger than anything restic
# gains, so it is asserted directly rather than left to the existing suite.

# %%
#|export
def test_a_plain_box_takes_the_untouched_path(monkeypatch, tmp_path):
    """
    A plain box must never enter the restic branch -- not "and behaves the same",
    but never calls it at all.
    """
    from tests.integration.conftest import create_boxyards
    import boxyard.cmds._sync_box as sync_mod

    remote_name, _root, cfg, cp, _data = create_boxyards()

    idx = new_box(config_path=cp, box_name="plainbox",
                  storage_location=remote_name, claim=False)
    (data_of(cfg, idx) / "a.txt").write_text("plain\n")

    called = []
    import boxyard._restic_sync as restic_sync
    real = restic_sync.sync_data_restic

    async def spy(*a, **k):
        called.append(True)
        return await real(*a, **k)

    monkeypatch.setattr(restic_sync, "sync_data_restic", spy)

    results = run(sync_box(config_path=cp, box_index_name=idx, verbose=False))

    assert called == [], "a plain box reached the restic branch"
    assert BoxPart.DATA in results


# %% [markdown]
# ## The ordinary conditions

# %%
#|export
def test_an_unchanged_box_reports_synced_and_transfers_nothing(pair):
    results = sync(pair)
    assert data_condition(results) is SyncCondition.SYNCED
    assert results[BoxPart.DATA][1] is False


def test_a_local_edit_pushes(pair):
    (pair["dataA"] / "notes.md").write_text("second\n")
    results = sync(pair)
    assert data_condition(results) is SyncCondition.NEEDS_PUSH
    assert results[BoxPart.DATA][1] is True
    assert sync(pair)[BoxPart.DATA][0].sync_condition is SyncCondition.SYNCED


def test_the_other_machine_pulls_it(pair):
    (pair["dataA"] / "notes.md").write_text("second\n")
    (pair["dataA"] / "sub" / "added.txt").write_text("new\n")
    sync(pair)

    results = sync(pair, "B")
    assert data_condition(results) is SyncCondition.NEEDS_PULL
    assert results[BoxPart.DATA][1] is True
    assert tree(pair["dataB"]) == tree(pair["dataA"])


def test_a_deletion_reaches_the_other_machine(pair):
    (pair["dataA"] / "sub" / "keep.txt").unlink()
    sync(pair)
    sync(pair, "B")
    assert not (pair["dataB"] / "sub" / "keep.txt").exists()
    assert tree(pair["dataB"]) == tree(pair["dataA"])


def test_a_box_not_included_here_is_excluded_not_pulled(pair):
    """
    Pulling it would undo `boxyard exclude`, exactly as for a plain box.

    Note this is reached through the real `exclude` command, not by deleting the
    directory: a box whose placement record survives but whose directory is gone
    is a MISSING checkout, which `sync_box` refuses long before the format
    matters. Same for plain and restic.
    """
    from boxyard.cmds import exclude_box

    run(exclude_box(config_path=pair["cpB"], box_index_name=pair["idx"]))
    results = sync(pair, "B")
    assert data_condition(results) is SyncCondition.EXCLUDED
    assert results[BoxPart.DATA][1] is False
    assert not pair["dataB"].exists()


def test_a_divergence_raises_and_changes_nothing(pair):
    """The steady-state CONFLICT, after B has adopted the converted box."""
    sync(pair, "B")  # B adopts, and now has a restic state record

    (pair["dataA"] / "notes.md").write_text("theirs\n")
    sync(pair)
    (pair["dataB"] / "notes.md").write_text("mine\n")

    with pytest.raises(SyncUnsafe) as excinfo:
        sync(pair, "B")
    assert "diverged" in str(excinfo.value)
    assert (pair["dataB"] / "notes.md").read_text() == "mine\n"


def test_a_replica_with_unpushed_work_refuses_to_adopt(pair):
    """
    The other side of adoption: a machine that held work the conversion never
    saw must not have it silently replaced. This is why converting a box whose
    replicas are all in sync matters.
    """
    import time

    time.sleep(0.01)
    (pair["dataB"] / "notes.md").write_text("work that predates the conversion\n")

    with pytest.raises(SyncUnsafe) as excinfo:
        sync(pair, "B")
    assert "converted to restic elsewhere" in str(excinfo.value)
    assert "discard-local" in str(excinfo.value)
    assert (pair["dataB"] / "notes.md").read_text() == (
        "work that predates the conversion\n"
    )


def test_a_replica_in_sync_adopts_cleanly(pair):
    results = sync(pair, "B")
    assert data_condition(results) is SyncCondition.NEEDS_PULL
    assert tree(pair["dataB"]) == tree(pair["dataA"])
    # ...and the pass after adoption is a no-op, not a spurious push.
    assert data_condition(sync(pair, "B")) is SyncCondition.SYNCED


def test_a_deletion_only_change_is_detected(pair):
    """
    `backup --dry-run` cannot see a deletion -- a file that is gone is simply
    not walked, so `files_new` and `files_changed` both stay 0. Without the file
    count in the state record a box whose only change is a deletion would report
    SYNCED forever and the deletion would never leave the machine.
    """
    sync(pair, "B")
    (pair["dataA"] / "sub" / "keep.txt").unlink()

    assert data_condition(sync(pair)) is SyncCondition.NEEDS_PUSH
    sync(pair, "B")
    assert not (pair["dataB"] / "sub" / "keep.txt").exists()


# %% [markdown]
# ## Ownership

# %%
#|export
def test_a_non_owner_holding_changes_is_write_denied_not_an_error(pair):
    """
    A condition and not an exception, for the same reason the plain path made it
    one: `multi-sync` runs every 1200s and a raise would manufacture the same
    unresolvable error 72 times a day.
    """
    from boxyard.cmds import claim_box

    run(claim_box(config_path=pair["cpA"], box_index_name=pair["idx"]))
    sync(pair, "B", sync_choices=[BoxPart.META])
    (pair["dataB"] / "notes.md").write_text("edited on the replica\n")

    results = sync(pair, "B")
    assert data_condition(results) is SyncCondition.WRITE_DENIED
    assert results[BoxPart.DATA][1] is False
    assert results[BoxPart.DATA][0].error_message


def test_a_non_owner_with_nothing_to_push_still_pulls(pair):
    from boxyard.cmds import claim_box

    run(claim_box(config_path=pair["cpA"], box_index_name=pair["idx"]))
    (pair["dataA"] / "notes.md").write_text("owner's edit\n")
    sync(pair)

    results = sync(pair, "B")
    assert data_condition(results) is SyncCondition.NEEDS_PULL
    assert tree(pair["dataB"]) == tree(pair["dataA"])


# %% [markdown]
# ## Concurrent pushers
#
# With the canonical path both machines record the SAME snapshot path, so the
# two pushes are siblings and the pointer is last-write-wins. The pointer is
# therefore re-read immediately before it is written.

# %%
#|export
def test_a_push_that_races_another_reports_conflict_and_keeps_both(pair):
    """
    B pushes while A's push is in flight. A must NOT overwrite B's pointer --
    that would silently replace B's snapshot with A's, and B's next pass would
    see a clean tree, report NEEDS_PULL, and lose its work from disk.
    """
    import boxyard._restic_sync as mod
    from boxyard._restic import push as real_push

    sync(pair, "B")  # B adopts, so both machines are steady-state

    (pair["dataA"] / "notes.md").write_text("A's work\n")
    (pair["dataB"] / "notes.md").write_text("B's work\n")

    fired = []

    async def push_then_let_b_win(*a, **k):
        result = await real_push(*a, **k)
        # B completes its whole push while A is between backup and pointer.
        # ONCE only: B's push goes through this same hook, so an unguarded
        # version recurses forever. And `await`, not a nested `asyncio.run`,
        # which cannot be called from inside a running loop.
        if not fired:
            fired.append(True)
            from boxyard._models import get_boxyard_meta as _meta
            from boxyard._restic_sync import sync_data_restic as real_sync

            bm = _meta(pair["cfgB"]).by_index_name[pair["idx"]]
            await real_sync(pair["cfgB"], bm, pair["idx"], may_push=True)
        return result

    mod.push = push_then_let_b_win
    try:
        results = sync(pair)
    finally:
        mod.push = real_push

    assert data_condition(results) is SyncCondition.CONFLICT
    assert results[BoxPart.DATA][1] is False
    message = results[BoxPart.DATA][0].error_message
    assert "safe in the repository" in message
    # B's pointer stands, and A's local work is untouched on disk.
    assert (pair["dataA"] / "notes.md").read_text() == "A's work\n"


# %% [markdown]
# ## An interrupted restore
#
# Without the `pulling_from` marker the torn tree reads as local edits and the
# box reports a CONFLICT that no person caused.

# %%
#|export
def test_an_interrupted_restore_resumes_instead_of_conflicting(pair):
    (pair["dataA"] / "notes.md").write_text("moved on\n")
    sync(pair)

    # As a crash mid-restore leaves it: the marker set, the tree torn.
    state = read_state(pair["cfgB"].boxyard_data_path, pair["idx"])
    from boxyard._restic import mark_pull_started
    from boxyard._restic import read_pointer

    pointer_snapshot = run(
        read_pointer(
            pair["cfgB"].rclone_config_path,
            pair["remote_name"],
            pair["cfgB"].storage_locations[pair["remote_name"]].store_path,
            pair["idx"],
        )
    )["snapshot"]
    mark_pull_started(pair["cfgB"].boxyard_data_path, pair["idx"], pointer_snapshot)
    (pair["dataB"] / "notes.md").write_text("half-written\n")

    results = sync(pair, "B")

    assert data_condition(results) is SyncCondition.SYNC_FROM_REMOTE_INCOMPLETE
    assert results[BoxPart.DATA][1] is True
    assert tree(pair["dataB"]) == tree(pair["dataA"])
    assert "pulling_from" not in read_state(
        pair["cfgB"].boxyard_data_path, pair["idx"]
    )


def test_an_interrupted_restore_refuses_a_forced_push(pair):
    from boxyard._restic import mark_pull_started

    mark_pull_started(pair["cfgB"].boxyard_data_path, pair["idx"], "0" * 64)
    with pytest.raises(SyncUnsafe) as excinfo:
        sync(pair, "B", sync_direction=SyncDirection.PUSH)
    assert "torn" in str(excinfo.value)


# %% [markdown]
# ## Excludes
#
# A converted box must not silently start storing what the plain path removes.

# %%
#|export
def test_the_exclude_list_still_applies_after_conversion(pair):
    from boxyard._restic import (
        ResticRepo, rclone_program_for, repo_url_for_box,
        resolve_restic_password, run_restic, latest_snapshot_id,
    )

    venv = pair["dataA"] / ".venv"
    venv.mkdir()
    (venv / "junk.bin").write_text("should never be stored\n")
    (pair["dataA"] / "notes.md").write_text("real content\n")
    sync(pair)

    cfg = pair["cfgA"]
    repo = ResticRepo(
        url=repo_url_for_box(
            cfg.storage_locations[pair["remote_name"]].store_path,
            pair["remote_name"], pair["idx"]),
        password=resolve_restic_password(cfg),
        cache_dir=cfg.boxyard_data_path / "restic_cache",
        rclone_program=rclone_program_for(cfg.rclone_config_path),
    )
    snap = run(latest_snapshot_id(repo))
    _, listing, _ = run(run_restic(repo, ["ls", snap]))
    assert ".venv" not in listing
    assert "notes.md" in listing


# %% [markdown]
# ## A box that declares restic with no pointer

# %%
#|export
def test_a_half_converted_box_reports_error_not_a_guess(pair):
    """
    Guessing would mean pushing a plain tree over a repository, or the reverse.
    """
    store = pair["cfgA"].storage_locations[pair["remote_name"]].store_path
    pointer = (pair["remote_root"] / "boxyard" / const.REMOTE_BOXES_REL_PATH
               / pair["idx"] / const.BOX_SNAPSHOT_POINTER_REL_PATH)
    pointer.unlink()

    results = sync(pair)
    assert data_condition(results) is SyncCondition.ERROR
    assert "data.snapshot" in results[BoxPart.DATA][0].error_message


# %% [markdown]
# ## The multi-sync board
#
# It is what Lukas watches a pass through, so a restic box's results must render
# with no special case.

# %%
#|export
def test_the_board_renders_a_restic_box(pair):
    from boxyard._models import SyncCondition as SC

    results = sync(pair)
    status, happened = results[BoxPart.DATA]
    # The exact shape the board reads: a SyncStatus with a known condition, and
    # a bool. Nothing else is consulted for a non-WRITE_DENIED box.
    assert isinstance(happened, bool)
    assert isinstance(status.sync_condition, SC)
    assert status.sync_condition.value in {c.value for c in SC}
    assert isinstance(status.is_dir, bool)
    assert status.local_sync_record is not None
    assert status.remote_sync_record is not None


def test_every_part_is_present_in_the_result(pair):
    """`multi-sync` indexes results by every requested part."""
    results = sync(pair)
    for part in (BoxPart.META, BoxPart.CONF, BoxPart.DATA):
        assert part in results
        assert len(results[part]) == 2

# %% [markdown]
# ## The ordinary push stamps agreement BEFORE the push
#
# 0.8.1 fixed the late `synced_at_unix` stamp at three push sites and
# mutation-verified only `convert_box` — removing `now_unix=` from THIS site,
# the one every ordinary restic sync goes through, left 186 tests green. Same
# spy pattern as `test_convert_box`: the unit test pins the mechanism, this
# pins the call site.

# %%
#|export
def test_an_ordinary_push_stamps_agreement_before_the_push(pair, monkeypatch):
    """
    `synced_at_unix` must predate the push, or anything written while the push
    reads the tree is permanently invisible to change detection — the snapshot
    predates it, the stamp postdates it.
    """
    import time

    import boxyard._restic_sync as mod

    seen = {}
    real = mod.write_state

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(mod, "write_state", spy)

    (pair["dataA"] / "notes.md").write_text("second\n")
    before = time.time()
    results = sync(pair)
    after = time.time()

    assert data_condition(results) is SyncCondition.NEEDS_PUSH
    assert "now_unix" in seen, (
        "the ordinary push called write_state without now_unix, so the state "
        "records the moment the push ENDED -- anything written while the push "
        "was reading the tree becomes permanently invisible"
    )
    assert before <= seen["now_unix"] <= after

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `--skip-unchanged` — a full pass puts an untouched box aside
#
# `_dev/FULL-PASS-SKIP-DESIGN-NOTE.md` (v5) is the design. A box is dropped
# from a pass only when EVERY part in the closure of what the pass would execute
# is provably unchanged on both sides:
#
# - the remote record's md5, from ONE hashed bulk listing of `sync_records/`,
#   is the md5 this machine remembered when the real path last agreed with that
#   record (`<part>.remote.json`, written by `_remote_identity`);
# - for a part this machine holds, the local record is complete, names the
#   ULID the sidecar does, and the local tree matches the fingerprint baseline.
#
# Nothing is stamped from a listing and nothing is written to the remote, so
# there is no observation window to race: any writer -- any version -- that
# rewrites a record changes its md5, and the equality fails on the next pass.
#
# Every "needed" verdict here is checked at the filter (`boxes_needing_sync_full`)
# and, where the behaviour lives in `multi-sync`, through the real CLI with
# `--print-skipped` on, so that ABSENCE from the output means "dropped from the
# pass" and nothing else. The restic DATA filter keeps its pointer-stamp logic;
# its tests are at the end.

# %%
#|default_exp integration.cmds.test_skip_unchanged

# %%
#|export
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from ulid import ULID

import boxyard.cmds as cmds_module
from boxyard import const
from boxyard._checkout import (
    CheckoutPlacement,
    LocalCheckoutState,
    PlacementState,
    RelocationPhase,
    RelocationRecord,
    get_box_checkout_status,
    save_placement,
)
from boxyard._enums import BoxPart, StorageFormat
from boxyard._fingerprint import base_path_for
from boxyard._models import BoxMeta, SyncCondition, SyncRecord, get_boxyard_meta
from boxyard._remote_identity import read_remote_identity, remote_identity_path
from boxyard._sync_policy import (
    RemoteBoxView,
    boxes_needing_sync_full,
    data_boxes_needing_sync,
    write_check_record,
)
from boxyard._tombstones import create_tombstone
from boxyard._utils import get_rclone_binary
from boxyard.cmds import (
    claim_box,
    convert_box,
    exclude_box,
    include_box,
    modify_boxmeta,
    new_box,
    sync_box,
    sync_missing_boxmetas,
)
from boxyard.config import get_config

pytestmark = pytest.mark.integration


def run(coro):
    return asyncio.run(coro)


needs_restic = pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic binary not available"
)


# %% [markdown]
# ## Fixtures and the filter oracle
#
# `verdict` feeds `boxes_needing_sync_full` exactly what `multi-sync` feeds it,
# built from the remote's files on disk (the remote is an alias to a local
# directory): the md5 of every `<part>.rec`, which is what `lsjson --hash`
# reports for a local backend, and the `boxes/` view.

# %%
#|export
def _store(remote_root: Path) -> Path:
    return remote_root / "boxyard"


def _key(sl: str, index_name: str):
    return (sl, index_name)


def remote_records(remote_root: Path, sl: str):
    rec_root = _store(remote_root) / const.SYNC_RECORDS_REL_PATH
    out = {}
    if rec_root.is_dir():
        for box_dir in sorted(rec_root.iterdir()):
            if not box_dir.is_dir():
                continue
            for f in box_dir.iterdir():
                if f.is_file() and f.name.endswith(".rec") and f.name.count(".") == 1:
                    out.setdefault(_key(sl, box_dir.name), {})[f.name[: -len(".rec")]] = (
                        hashlib.md5(f.read_bytes()).hexdigest()
                    )
    return out


def remote_boxes(remote_root: Path, sl: str):
    boxes = _store(remote_root) / const.REMOTE_BOXES_REL_PATH
    out = {}
    if boxes.is_dir():
        for d in boxes.iterdir():
            if not d.is_dir():
                continue
            pointer = d / const.BOX_SNAPSHOT_POINTER_REL_PATH
            out[_key(sl, d.name)] = RemoteBoxView(
                index_name=d.name,
                boxmeta=(d / const.BOX_METAFILE_REL_PATH).is_file(),
                conf_dir=(d / const.BOX_CONF_REL_PATH).is_dir(),
                pointer=(
                    (None, pointer.stat().st_size) if pointer.is_file() else None
                ),
            )
    return out


def remote_record_dir(remote_root: Path, idx: str) -> Path:
    return _store(remote_root) / const.SYNC_RECORDS_REL_PATH / idx


def verdict(
    config_path,
    remote_root: Path,
    requested=None,
    *,
    skip_meta: bool = True,
    skip_data: bool = True,
    tombstoned: set[str] | None = None,
    records=None,
    boxes=None,
    pointer_override=None,
):
    config = get_config(config_path)
    metas = get_boxyard_meta(config).box_metas
    sl = metas[0].storage_location if metas else "test_remote"
    _boxes = remote_boxes(remote_root, sl) if boxes is None else boxes
    if pointer_override is not None:
        for view in _boxes.values():
            if view.index_name in pointer_override:
                view.pointer = pointer_override[view.index_name]
    return boxes_needing_sync_full(
        config,
        metas,
        requested_parts=list(BoxPart) if requested is None else requested,
        records=remote_records(remote_root, sl) if records is None else records,
        boxes=_boxes,
        tombstoned=tombstoned or set(),
        skip_meta=skip_meta,
        skip_data=skip_data,
    )


def _multi_sync(config_path, *args):
    from typer.testing import CliRunner

    from boxyard._cli.app import app

    result = CliRunner().invoke(
        app, ["--config", str(config_path), "multi-sync", *args]
    )
    assert result.exit_code == 0, f"exited {result.exit_code}\n{result.output}"
    return result


@pytest.fixture
def yard(temp_boxyard, monkeypatch, tmp_path):
    """One machine, one settled plain box: synced once, so records, sidecars
    and baselines all exist and the box is provable for every part."""
    remote_name, remote_root, config, config_path, _dp = temp_boxyard
    monkeypatch.setenv("BOXYARD_RESTIC_PASSWORD", "skip-test-password")
    for target in ("boxyard.const", "boxyard._restic.const"):
        monkeypatch.setattr(f"{target}.RESTIC_CANONICAL_ROOT", str(tmp_path / "canon"))

    idx = new_box(config_path=config_path, box_name="skipbox",
                  storage_location=remote_name, claim=False)
    bm = get_boxyard_meta(config).by_index_name[idx]
    data = bm.get_local_part_path(config, BoxPart.DATA)
    (data / "notes.md").write_text("first\n")
    (data / "sub").mkdir()
    (data / "sub" / "inner.txt").write_text("inner\n")
    (data / "sub" / "link").symlink_to("inner.txt")
    run(sync_box(config_path=config_path, box_index_name=idx, verbose=False))
    return {
        "idx": idx, "box_id": bm.box_id, "config_path": config_path,
        "remote_name": remote_name, "remote_root": remote_root, "data": data,
    }


def metas(yard):
    return get_boxyard_meta(get_config(yard["config_path"])).box_metas


def box_meta(yard) -> BoxMeta:
    return get_boxyard_meta(get_config(yard["config_path"])).by_index_name[yard["idx"]]


def local_record_path(yard, part: BoxPart) -> Path:
    return box_meta(yard).get_local_sync_record_path(get_config(yard["config_path"]), part)


# %% [markdown]
# ## The feature: a settled plain box is put aside

# %%
#|export
def test_a_settled_plain_box_is_provable_for_every_part(yard):
    v = verdict(yard["config_path"], yard["remote_root"])
    assert v.skippable == [yard["idx"]], v.reasons
    assert v.needed == []
    for part in BoxPart:
        ident = read_remote_identity(local_record_path(yard, part))
        assert ident is not None and ident["sync_complete"], part


def test_a_settled_plain_box_is_dropped_from_a_full_pass(yard):
    """Through the real CLI. `--print-skipped` is on, so a box that merely
    synced with no change would still be printed; absence means dropped."""
    first = _multi_sync(yard["config_path"], "--skip-unchanged", "--print-skipped")
    assert yard["idx"] not in first.output, first.output
    assert "1 box(es) provably unchanged" in first.output


def test_the_first_pass_after_an_upgrade_syncs_and_the_second_skips(yard):
    """No sidecars (a machine that has never run this version): every box goes
    through the real path once, which writes them; the next pass skips."""
    for part in BoxPart:
        remote_identity_path(local_record_path(yard, part)).unlink()
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "meta"

    first = _multi_sync(yard["config_path"], "--skip-unchanged", "--print-skipped")
    assert yard["idx"] in first.output
    second = _multi_sync(yard["config_path"], "--skip-unchanged", "--print-skipped")
    assert yard["idx"] not in second.output


def _touch_same_content(p: Path):
    p.write_text(p.read_text())


def _chmod_x(p: Path):
    os.chmod(p, 0o755)


def _retarget_link(p: Path):
    p.unlink()
    p.symlink_to("../notes.md")


@pytest.mark.parametrize(
    "shape,mutate",
    [
        ("edit content", lambda d: (d / "notes.md").write_text("second\n")),
        ("add a file", lambda d: (d / "new.md").write_text("new\n")),
        ("delete a file", lambda d: (d / "notes.md").unlink()),
        ("rename a file", lambda d: (d / "notes.md").rename(d / "renamed.md")),
        ("chmod +x", lambda d: _chmod_x(d / "notes.md")),
        ("touch, same content", lambda d: _touch_same_content(d / "notes.md")),
        ("delete a directory", lambda d: shutil.rmtree(d / "sub")),
        ("add a symlink", lambda d: (d / "l2").symlink_to("notes.md")),
        ("remove a symlink", lambda d: (d / "sub" / "link").unlink()),
        ("retarget a symlink", lambda d: _retarget_link(d / "sub" / "link")),
    ],
)
def test_every_change_shape_on_a_settled_box_is_needed(yard, shape, mutate):
    """The ten shapes the 0.8.x arc exists for, including the ones that leave
    no newer file mtime behind. Each must route the box to the real path with
    DATA named as the unprovable part."""
    mutate(yard["data"])
    v = verdict(yard["config_path"], yard["remote_root"])
    assert v.needed == [yard["idx"]], shape
    assert v.reasons[yard["idx"]] == "data", shape


def test_a_local_meta_edit_is_needed(yard):
    modify_boxmeta(config_path=yard["config_path"], box_index_name=yard["idx"],
                   modifications={"groups": ["a-new-group"]})
    assert verdict(yard["config_path"], yard["remote_root"]).reasons.get(yard["idx"]) == "meta"


# %% [markdown]
# ## The remote side: identity is the record's bytes

# %%
#|export
def _rewrite_remote_record(yard, part: BoxPart, *, sync_complete=True, hostname="elsewhere"):
    """What another machine's push leaves behind: a new record (new ULID) in
    place of the old one. Written through the production writer."""
    config = get_config(yard["config_path"])
    rec = SyncRecord.create(sync_complete=sync_complete, syncer_hostname=hostname)
    run(rec.rclone_save(
        str(config.rclone_config_path), yard["remote_name"],
        box_meta(yard).get_remote_sync_record_path(config, part).as_posix(),
    ))
    return rec


@pytest.mark.parametrize("part", list(BoxPart))
def test_a_record_rewritten_elsewhere_is_needed(yard, part):
    _rewrite_remote_record(yard, part)
    v = verdict(yard["config_path"], yard["remote_root"])
    assert v.reasons.get(yard["idx"]) == part.value


def test_a_record_rewritten_by_an_older_boxyard_is_needed(yard):
    """An older writer knows nothing about sidecars or hashes; it simply
    writes `data.rec` with plain rclone. The md5 changes, so the box is
    needed -- there is no version barrier to get wrong."""
    config = get_config(yard["config_path"])
    old = SyncRecord.create(sync_complete=True, syncer_hostname="old-machine")
    target = remote_record_dir(yard["remote_root"], yard["idx"]) / "data.rec"
    target.write_text(old.model_dump_json(indent=2))  # a different serializer, even
    assert verdict(yard["config_path"], yard["remote_root"]).reasons.get(yard["idx"]) == "data"


def test_a_same_ulid_record_with_different_bytes_is_needed(yard):
    """Identity is the bytes, not the ULID: rewriting the same record with
    other formatting is 'moved' until this machine reads it again."""
    target = remote_record_dir(yard["remote_root"], yard["idx"]) / "meta.rec"
    rec = SyncRecord.model_validate_json(target.read_text())
    target.write_text(rec.model_dump_json(indent=4))
    assert verdict(yard["config_path"], yard["remote_root"]).reasons.get(yard["idx"]) == "meta"

    # ...and the real path re-agrees with the new bytes on the next visit.
    run(sync_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    assert verdict(yard["config_path"], yard["remote_root"]).skippable == [yard["idx"]]


def test_a_box_absent_from_the_records_listing_is_needed(yard):
    v = verdict(yard["config_path"], yard["remote_root"], records={})
    assert v.needed == [yard["idx"]]
    assert v.reasons[yard["idx"]] == "meta"


def test_a_record_the_backend_could_not_hash_is_needed(yard):
    sl = yard["remote_name"]
    records = remote_records(yard["remote_root"], sl)
    records[_key(sl, yard["idx"])]["data"] = None
    assert verdict(yard["config_path"], yard["remote_root"], records=records).reasons[yard["idx"]] == "data"


def test_a_record_absent_from_the_remote_is_needed_for_a_held_part(yard):
    """Marker-era finding: an identity for a record that no longer exists.
    Here the identity IS the record, so its absence is the verdict."""
    (remote_record_dir(yard["remote_root"], yard["idx"]) / "data.rec").unlink()
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "data"


def test_a_stale_sidecar_cannot_prove(yard):
    """The sidecar remembers OLD bytes; the listing shows new ones."""
    _rewrite_remote_record(yard, BoxPart.META)
    ident = read_remote_identity(local_record_path(yard, BoxPart.META))
    assert ident is not None  # still there, and useless
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "meta"


def test_two_stores_cannot_lend_each_other_a_proof(yard):
    """Views are keyed by (storage location, index name): a same-named box on
    another store with matching records proves nothing about this one."""
    sl = yard["remote_name"]
    records = {
        ("some-other-store", idx): recs
        for (_s, idx), recs in remote_records(yard["remote_root"], sl).items()
    }
    v = verdict(yard["config_path"], yard["remote_root"], records=records)
    assert v.reasons[yard["idx"]] == "meta"


def test_a_box_renamed_on_the_remote_is_judged_under_its_remote_name(yard):
    """`sync_box` resolves the remote by box ID, so a box another machine
    renamed is synced under the NEW name while this machine still calls it by
    the old one. The filter keys both listings by id for the same reason."""
    store = _store(yard["remote_root"])
    new_name = yard["box_id"] + "__renamed"
    (store / const.REMOTE_BOXES_REL_PATH / yard["idx"]).rename(store / const.REMOTE_BOXES_REL_PATH / new_name)
    (store / const.SYNC_RECORDS_REL_PATH / yard["idx"]).rename(store / const.SYNC_RECORDS_REL_PATH / new_name)
    v = verdict(yard["config_path"], yard["remote_root"])
    assert v.skippable == [yard["idx"]], v.reasons

    result = _multi_sync(yard["config_path"], "--skip-unchanged", "--print-skipped")
    assert yard["idx"] not in result.output


def test_a_half_renamed_box_is_needed(yard):
    """A rename whose `boxes/` move happened but whose `sync_records/` move
    did not (that failure is a verbose-only warning in `rename`): the real
    path resolves the box by id to the NEW name, finds no records there, and
    raises. The records under the OLD name must not lend the box a proof."""
    store = _store(yard["remote_root"])
    new_name = yard["box_id"] + "__renamed"
    (store / const.REMOTE_BOXES_REL_PATH / yard["idx"]).rename(store / const.REMOTE_BOXES_REL_PATH / new_name)
    v = verdict(yard["config_path"], yard["remote_root"])
    assert v.reasons.get(yard["idx"]) == "meta"


def test_two_remote_directories_with_the_same_id_are_needed(yard):
    """A copied box directory: the real path must sort it out, not the filter."""
    boxes = _store(yard["remote_root"]) / const.REMOTE_BOXES_REL_PATH
    shutil.copytree(boxes / yard["idx"], boxes / (yard["box_id"] + "__copy"))
    v = verdict(yard["config_path"], yard["remote_root"])
    assert v.reasons.get(yard["idx"]) == "remote-box-absent"


def test_a_box_whose_id_the_remote_no_longer_lists_is_needed(yard):
    """Deleted remotely without a tombstone, or a store the listing did not
    cover: no `boxmeta.toml` under this id anywhere, so the real path decides."""
    boxes = _store(yard["remote_root"]) / const.REMOTE_BOXES_REL_PATH
    shutil.rmtree(boxes / yard["idx"])
    v = verdict(yard["config_path"], yard["remote_root"])
    assert v.reasons[yard["idx"]] == "remote-box-absent"


@pytest.mark.parametrize("part", [BoxPart.META, BoxPart.DATA])
def test_a_missing_baseline_is_never_proof(yard, part):
    base_path_for(local_record_path(yard, part)).unlink()
    assert verdict(yard["config_path"], yard["remote_root"]).reasons.get(yard["idx"]) == part.value


def test_an_incomplete_local_record_is_never_proof(yard):
    p = local_record_path(yard, BoxPart.DATA)
    rec = SyncRecord.model_validate_json(p.read_text())
    p.write_text(rec.model_copy(update={"sync_complete": False}).model_dump_json())
    assert verdict(yard["config_path"], yard["remote_root"]).reasons.get(yard["idx"]) == "data"


def test_a_sidecar_naming_another_ulid_than_the_local_record_is_never_proof(yard):
    """Local record and sidecar must agree on WHICH record this machine holds."""
    p = local_record_path(yard, BoxPart.META)
    rec = SyncRecord.model_validate_json(p.read_text())
    p.write_text(rec.model_copy(update={"ulid": ULID()}).model_dump_json())
    assert verdict(yard["config_path"], yard["remote_root"]).reasons.get(yard["idx"]) == "meta"


# %% [markdown]
# ## Each flag gates its own parts

# %%
#|export
def test_the_whole_box_flag_covers_a_meta_only_pass(yard):
    result = _multi_sync(yard["config_path"], "-c", "meta", "--skip-unchanged", "--print-skipped")
    assert yard["idx"] not in result.output, result.output


def test_the_meta_flag_never_drops_a_box_from_a_data_pass(yard):
    full = _multi_sync(yard["config_path"], "--skip-unchanged-meta", "--print-skipped")
    assert "no box was skipped" in full.output
    assert yard["idx"] in full.output

    data = _multi_sync(yard["config_path"], "-c", "data", "--skip-unchanged-meta", "--print-skipped")
    assert "no box was skipped" in data.output
    assert yard["idx"] in data.output

    v = verdict(yard["config_path"], yard["remote_root"], skip_data=False)
    assert v.reasons[yard["idx"]] == "conf"  # META proved, then the first unflagged part


def test_the_no_skip_message_names_the_unprovable_part(yard):
    (yard["data"] / "notes.md").write_text("edited\n")
    result = _multi_sync(yard["config_path"], "--skip-unchanged", "--print-skipped")
    assert "no box was skipped" in result.output
    assert "'data': 1" in result.output, result.output
    assert yard["idx"] in result.output


# %% [markdown]
# ## One box cannot take the pass down; a listing failure costs only the optimisation

# %%
#|export
def test_a_box_that_cannot_be_judged_is_synced_not_dropped(yard, monkeypatch, capsys):
    config_path, remote_name = yard["config_path"], yard["remote_name"]
    other = new_box(config_path=config_path, box_name="other",
                    storage_location=remote_name, claim=False)
    run(sync_box(config_path=config_path, box_index_name=other, verbose=False))

    import boxyard._sync_policy as policy

    real = policy.meta_provably_unchanged

    def _judge(config, bm, recs):
        if bm.index_name == yard["idx"]:
            raise OSError("simulated unreadable directory")
        return real(config, bm, recs)

    monkeypatch.setattr(policy, "meta_provably_unchanged", _judge)
    v = verdict(config_path, yard["remote_root"])
    assert v.needed == [yard["idx"]]
    assert v.reasons[yard["idx"]].startswith("error:")
    assert v.skippable == [other]
    assert yard["idx"] in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unreadable_directory_isolates_to_its_own_box(yard):
    config_path, remote_name = yard["config_path"], yard["remote_name"]
    other = new_box(config_path=config_path, box_name="other",
                    storage_location=remote_name, claim=False)
    run(sync_box(config_path=config_path, box_index_name=other, verbose=False))

    locked = yard["data"] / "sub"
    os.chmod(locked, 0o000)
    try:
        v = verdict(config_path, yard["remote_root"])
    finally:
        os.chmod(locked, 0o755)
    assert v.needed == [yard["idx"]], v.reasons
    assert v.skippable == [other]


def test_a_failed_bulk_listing_syncs_everything_and_says_so(yard, monkeypatch):
    import boxyard._utils.rclone as rclone_module
    from boxyard._utils.rclone import RcloneFailed

    real = rclone_module.rclone_lsjson

    async def _failing(rclone_config_path, source, source_path, **kwargs):
        if kwargs.get("md5"):
            raise RcloneFailed(["rclone", "lsjson"], 1, "", "simulated: connection reset")
        return await real(rclone_config_path, source, source_path, **kwargs)

    monkeypatch.setattr("boxyard._utils.rclone_lsjson", _failing)
    result = _multi_sync(yard["config_path"], "--skip-unchanged", "--print-skipped")
    assert "a bulk listing failed, syncing every box" in result.output
    assert yard["idx"] in result.output


def test_multi_sync_refuses_a_running_event_loop(yard):
    """It used to print 'syncing every box instead of filtering' and then run
    nothing at all (zero sync_box calls, exit 0)."""
    from typer.testing import CliRunner

    from boxyard._cli.app import app

    async def _inside_a_loop():
        return CliRunner().invoke(
            app, ["--config", str(yard["config_path"]), "multi-sync", "--skip-unchanged"]
        )

    result = run(_inside_a_loop())
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)
    assert "running event loop" in str(result.exception)


# %% [markdown]
# ## Placement decides DATA first, by EXACT state

# %%
#|export
def test_an_excluded_box_with_nothing_on_disk_is_provable(yard):
    run(exclude_box(config_path=yard["config_path"], box_index_name=yard["idx"]))
    assert not yard["data"].exists()
    # The exclusion itself agreed with the remote DATA record (EXCLUDED verdict).
    run(sync_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    v = verdict(yard["config_path"], yard["remote_root"])
    assert v.skippable == [yard["idx"]], v.reasons

    result = _multi_sync(yard["config_path"], "--skip-unchanged", "--print-skipped")
    assert yard["idx"] not in result.output


def test_an_excluded_box_with_a_tree_on_disk_is_needed(yard):
    run(exclude_box(config_path=yard["config_path"], box_index_name=yard["idx"]))
    yard["data"].mkdir(parents=True)
    (yard["data"] / "stray.txt").write_text("someone put this here\n")
    assert verdict(yard["config_path"], yard["remote_root"]).reasons.get(yard["idx"]) == "data"


def test_an_excluded_box_whose_remote_data_moved_is_needed(yard):
    run(exclude_box(config_path=yard["config_path"], box_index_name=yard["idx"]))
    run(sync_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    assert verdict(yard["config_path"], yard["remote_root"]).skippable == [yard["idx"]]
    _rewrite_remote_record(yard, BoxPart.DATA)
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "data"


def test_an_excluded_box_with_an_incomplete_remote_push_is_needed(yard):
    """Another machine's push crashed after writing its incomplete record.
    The real path answers INCOMPLETE (and raises) before it ever reaches
    EXCLUDED, so the filter must not call the box provable either."""
    run(exclude_box(config_path=yard["config_path"], box_index_name=yard["idx"]))
    run(sync_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    assert verdict(yard["config_path"], yard["remote_root"]).skippable == [yard["idx"]]

    _rewrite_remote_record(yard, BoxPart.DATA, sync_complete=False)
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "data"
    with pytest.raises(Exception, match="incomplete"):
        run(sync_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    # ...and the raise wrote no sidecar for it.
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "data"


def _make_missing(yard):
    shutil.rmtree(yard["data"])


def _make_unavailable(yard):
    save_placement(
        get_config(yard["config_path"]), yard["box_id"],
        CheckoutPlacement(checkout_root="no-such-root", state=PlacementState.INCLUDED),
    )


def _make_relocating(yard):
    save_placement(
        get_config(yard["config_path"]), yard["box_id"],
        CheckoutPlacement(
            checkout_root="default", state=PlacementState.RELOCATING,
            relocation=RelocationRecord(
                source_root="default", destination_root="default",
                phase=RelocationPhase.COPYING,
            ),
        ),
    )


@pytest.mark.parametrize(
    "state,arrange",
    [
        (LocalCheckoutState.MISSING, _make_missing),
        (LocalCheckoutState.UNAVAILABLE, _make_unavailable),
        (LocalCheckoutState.RELOCATING, _make_relocating),
    ],
)
def test_every_other_placement_state_is_needed(yard, state, arrange):
    arrange(yard)
    config = get_config(yard["config_path"])
    assert get_box_checkout_status(config, box_meta(yard)).state is state
    assert verdict(yard["config_path"], yard["remote_root"]).reasons.get(yard["idx"]) == "data", state


# %% [markdown]
# ## CONF: absent on this machine

# %%
#|export
def _strip_conf(yard):
    """`new_box` creates an empty conf/ and the first sync records it, so the
    fixture's CONF is a real (empty) synced part. Construct never-had-CONF:
    no directory and no record on either side."""
    config = get_config(yard["config_path"])
    bm = box_meta(yard)
    shutil.rmtree(bm.get_local_part_path(config, BoxPart.CONF))
    rec = bm.get_local_sync_record_path(config, BoxPart.CONF)
    rec.unlink()
    base_path_for(rec).unlink()
    remote_identity_path(rec).unlink()
    (remote_record_dir(yard["remote_root"], yard["idx"]) / "conf.rec").unlink()


def test_never_had_conf_is_provable_and_every_asymmetry_is_needed(yard):
    config = get_config(yard["config_path"])
    bm = box_meta(yard)
    _strip_conf(yard)
    assert verdict(yard["config_path"], yard["remote_root"]).skippable == [yard["idx"]]

    # A remote conf tree without its record -- a loud ERROR on the real path.
    remote_conf = _store(yard["remote_root"]) / const.REMOTE_BOXES_REL_PATH / yard["idx"] / const.BOX_CONF_REL_PATH
    remote_conf.mkdir()
    (remote_conf / const.RCLONE_EXCLUDE_FILENAME).write_text("*.log\n")
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "conf"
    shutil.rmtree(remote_conf)

    # A local conf directory nothing has synced yet.
    local_conf = bm.get_local_part_path(config, BoxPart.CONF)
    local_conf.mkdir(parents=True)
    (local_conf / const.RCLONE_EXCLUDE_FILENAME).write_text("*.log\n")
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "conf"
    shutil.rmtree(local_conf)

    # A remote conf record this machine has never read: needed until the real
    # path agrees with it (which it does -- both sides absent reads SYNCED).
    _rewrite_remote_record(yard, BoxPart.CONF)
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "conf"
    run(sync_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    assert verdict(yard["config_path"], yard["remote_root"]).skippable == [yard["idx"]]

    # ...but an INCOMPLETE remote conf record is a push interrupted elsewhere.
    _rewrite_remote_record(yard, BoxPart.CONF, sync_complete=False)
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "conf"


def test_a_synced_conf_is_provable_and_a_conf_edit_is_needed(yard):
    config = get_config(yard["config_path"])
    local_conf = box_meta(yard).get_local_part_path(config, BoxPart.CONF)
    local_conf.mkdir(parents=True, exist_ok=True)
    (local_conf / const.RCLONE_EXCLUDE_FILENAME).write_text("*.log\n")
    run(sync_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    assert verdict(yard["config_path"], yard["remote_root"]).skippable == [yard["idx"]]

    (local_conf / const.RCLONE_EXCLUDE_FILENAME).write_text("*.log\n*.tmp\n")
    assert verdict(yard["config_path"], yard["remote_root"]).reasons[yard["idx"]] == "conf"


# %% [markdown]
# ## A tombstoned box always reaches `sync_box`, whose warning is the report

# %%
#|export
def test_a_tombstoned_box_is_needed_even_when_its_files_survive(yard):
    config = get_config(yard["config_path"])
    run(create_tombstone(config, yard["remote_name"], yard["box_id"], yard["idx"]))
    assert (_store(yard["remote_root"]) / const.REMOTE_BOXES_REL_PATH / yard["idx"]).is_dir()

    v = verdict(yard["config_path"], yard["remote_root"], tombstoned={yard["idx"]})
    assert v.reasons.get(yard["idx"]) == "tombstoned"

    result = _multi_sync(yard["config_path"], "--skip-unchanged", "--print-skipped")
    assert "was deleted" in result.output, result.output


# %% [markdown]
# ## Two machines: the races and the closure
#
# `fleet` is machine A and machine B sharing one remote, both with the box
# included and settled. The "foreign push" tests inject A's push INSIDE B's
# filtered pass, right after B's own sync of the box -- the window a pass-end
# stamp would have adopted -- and require the box to be needed on B's next
# pass and the pushed change to arrive.

# %%
#|export
@pytest.fixture
def fleet(monkeypatch, tmp_path):
    from tests.integration.conftest import create_boxyards

    monkeypatch.setenv("BOXYARD_RESTIC_PASSWORD", "skip-test-password")
    for target in ("boxyard.const", "boxyard._restic.const"):
        monkeypatch.setattr(f"{target}.RESTIC_CANONICAL_ROOT", str(tmp_path / "canon"))

    remote_name, remote_root, yards = create_boxyards(num_boxyards=2)
    (cfgA, cpA, _), (cfgB, cpB, _) = yards
    idx = new_box(config_path=cpA, box_name="shared", storage_location=remote_name, claim=False)
    bmA = get_boxyard_meta(cfgA).by_index_name[idx]
    dataA = bmA.get_local_part_path(cfgA, BoxPart.DATA)
    (dataA / "notes.md").write_text("first\n")
    run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))

    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=idx, read_only=True))
    run(sync_box(config_path=cpB, box_index_name=idx, verbose=False))
    bmB = get_boxyard_meta(cfgB).by_index_name[idx]
    dataB = bmB.get_local_part_path(cfgB, BoxPart.DATA)
    assert (dataB / "notes.md").read_text() == "first\n"
    assert verdict(cpB, remote_root).skippable == [idx]

    return {
        "idx": idx, "remote_name": remote_name, "remote_root": remote_root,
        "cpA": cpA, "cpB": cpB, "dataA": dataA, "dataB": dataB,
    }


def _push_from_a_after_b_synced(fleet, monkeypatch, foreign_push):
    real = cmds_module.sync_box
    fired = []

    async def _wrapped(**kwargs):
        result = await real(**kwargs)
        if kwargs["box_index_name"] == fleet["idx"] and not fired:
            fired.append(True)
            await foreign_push(real)
        return result

    monkeypatch.setattr(cmds_module, "sync_box", _wrapped)
    result = _multi_sync(fleet["cpB"], "--skip-unchanged", "--print-skipped")
    monkeypatch.setattr(cmds_module, "sync_box", real)
    assert fired, "the box never went through sync_box in the injected pass"
    return result


def test_a_conf_recorded_but_never_materialized_is_provable(fleet):
    """`new_box` pushes an empty conf/, which rclone records but never creates
    on the remote. Every other machine holds no CONF record and no directory
    while the remote holds a record: the real path reads SYNCED and transfers
    nothing, and its agreement (the sidecar) is what makes it provable."""
    cfgB = get_config(fleet["cpB"])
    bmB = get_boxyard_meta(cfgB).by_index_name[fleet["idx"]]
    rec_path = bmB.get_local_sync_record_path(cfgB, BoxPart.CONF)
    assert not rec_path.exists()
    assert not bmB.get_local_part_path(cfgB, BoxPart.CONF).exists()
    assert read_remote_identity(rec_path) is not None
    assert verdict(fleet["cpB"], fleet["remote_root"]).skippable == [fleet["idx"]]


@pytest.mark.parametrize("shape", ["nested-only", "empty"])
def test_a_remote_conf_directory_of_any_shape_is_needed(fleet, shape):
    """A files-only depth-3 listing could not see `conf/nested/settings.txt`
    (depth 4) and called CONF absent; the box was skipped while an unfiltered
    sync pulled the file. The directory entry itself decides now."""
    remote_conf = _store(fleet["remote_root"]) / const.REMOTE_BOXES_REL_PATH / fleet["idx"] / const.BOX_CONF_REL_PATH
    if shape == "nested-only":
        (remote_conf / "nested").mkdir(parents=True)
        (remote_conf / "nested" / "settings.txt").write_text("s\n")
    else:
        remote_conf.mkdir()
    assert verdict(fleet["cpB"], fleet["remote_root"]).reasons.get(fleet["idx"]) == "conf"

    result = _multi_sync(fleet["cpB"], "--skip-unchanged", "--print-skipped")
    assert fleet["idx"] in result.output


def test_a_foreign_meta_push_after_b_synced_the_box_is_needed_next_pass(fleet, monkeypatch):
    (fleet["dataB"] / "from-b.md").write_text("b\n")

    async def _a_pushes_meta(real_sync_box):
        modify_boxmeta(config_path=fleet["cpA"], box_index_name=fleet["idx"],
                       modifications={"groups": ["pushed-by-a"]})
        await real_sync_box(config_path=fleet["cpA"], box_index_name=fleet["idx"],
                            sync_choices=[BoxPart.META], verbose=False)

    _push_from_a_after_b_synced(fleet, monkeypatch, _a_pushes_meta)

    assert verdict(fleet["cpB"], fleet["remote_root"]).reasons.get(fleet["idx"]) == "meta"
    nxt = _multi_sync(fleet["cpB"], "--skip-unchanged", "--print-skipped")
    assert fleet["idx"] in nxt.output
    bmB = get_boxyard_meta(get_config(fleet["cpB"])).by_index_name[fleet["idx"]]
    assert "pushed-by-a" in bmB.groups


def test_a_foreign_data_push_after_b_synced_the_box_is_needed_next_pass(fleet, monkeypatch):
    modify_boxmeta(config_path=fleet["cpB"], box_index_name=fleet["idx"],
                   modifications={"groups": ["edited-on-b"]})

    async def _a_pushes_data(real_sync_box):
        (fleet["dataA"] / "notes.md").write_text("second, from a\n")
        await real_sync_box(config_path=fleet["cpA"], box_index_name=fleet["idx"],
                            sync_choices=[BoxPart.DATA], verbose=False)

    _push_from_a_after_b_synced(fleet, monkeypatch, _a_pushes_data)

    assert verdict(fleet["cpB"], fleet["remote_root"]).reasons.get(fleet["idx"]) == "data"
    nxt = _multi_sync(fleet["cpB"], "--skip-unchanged", "--print-skipped")
    assert fleet["idx"] in nxt.output
    assert (fleet["dataB"] / "notes.md").read_text() == "second, from a\n"


def test_a_foreign_push_during_b_s_own_sync_of_the_box_is_needed_next_pass(fleet, monkeypatch):
    """The marker-era critical finding, re-run against the hash design: A
    pushes while B's real path is mid-way through the box. B's agreement is
    with the bytes it READ; A's record has other bytes; next pass is needed."""
    import boxyard._models as models_module

    # The box must go through the real path on B's pass for the race to
    # exist at all: a META edit on B makes it needed.
    modify_boxmeta(config_path=fleet["cpB"], box_index_name=fleet["idx"],
                   modifications={"groups": ["edited-on-b"]})
    real_status = models_module.get_sync_status
    fired = []

    async def _status_then_a_pushes(**kwargs):
        st = await real_status(**kwargs)
        if str(kwargs["remote_sync_record_path"]).endswith("data.rec") and not fired:
            fired.append(True)
            (fleet["dataA"] / "notes.md").write_text("pushed while b looked\n")
            await sync_box(config_path=fleet["cpA"], box_index_name=fleet["idx"],
                           sync_choices=[BoxPart.DATA], verbose=False)
        return st

    monkeypatch.setattr(models_module, "get_sync_status", _status_then_a_pushes)
    _multi_sync(fleet["cpB"], "--skip-unchanged", "--print-skipped")
    monkeypatch.setattr(models_module, "get_sync_status", real_status)
    assert fired

    assert verdict(fleet["cpB"], fleet["remote_root"]).reasons.get(fleet["idx"]) == "data"
    nxt = _multi_sync(fleet["cpB"], "--skip-unchanged", "--print-skipped")
    assert fleet["idx"] in nxt.output
    assert (fleet["dataB"] / "notes.md").read_text() == "pushed while b looked\n"


def test_a_data_pass_cannot_skip_a_box_whose_remote_conf_moved(fleet):
    cfgA = get_config(fleet["cpA"])
    confA = get_boxyard_meta(cfgA).by_index_name[fleet["idx"]].get_local_part_path(cfgA, BoxPart.CONF)
    confA.mkdir(parents=True, exist_ok=True)
    (confA / const.RCLONE_EXCLUDE_FILENAME).write_text("*.log\n")
    run(sync_box(config_path=fleet["cpA"], box_index_name=fleet["idx"], verbose=False))

    v = verdict(fleet["cpB"], fleet["remote_root"], requested=[BoxPart.DATA])
    assert v.reasons.get(fleet["idx"]) == "conf"

    result = _multi_sync(fleet["cpB"], "-c", "data", "--skip-unchanged", "--print-skipped")
    assert fleet["idx"] in result.output
    cfgB = get_config(fleet["cpB"])
    confB = get_boxyard_meta(cfgB).by_index_name[fleet["idx"]].get_local_part_path(cfgB, BoxPart.CONF)
    assert (confB / const.RCLONE_EXCLUDE_FILENAME).read_text() == "*.log\n"


def test_a_data_pass_cannot_skip_a_box_whose_remote_meta_moved(fleet):
    modify_boxmeta(config_path=fleet["cpA"], box_index_name=fleet["idx"],
                   modifications={"groups": ["pushed-by-a"]})
    run(sync_box(config_path=fleet["cpA"], box_index_name=fleet["idx"],
                 sync_choices=[BoxPart.META], verbose=False))

    v = verdict(fleet["cpB"], fleet["remote_root"], requested=[BoxPart.DATA])
    assert v.reasons.get(fleet["idx"]) == "meta"

    result = _multi_sync(fleet["cpB"], "-c", "data", "--skip-unchanged", "--print-skipped")
    assert fleet["idx"] in result.output
    bmB = get_boxyard_meta(get_config(fleet["cpB"])).by_index_name[fleet["idx"]]
    assert "pushed-by-a" in bmB.groups


def test_undoing_a_denied_edit_still_pulls_the_owners_push(fleet):
    """Identity, not the tree, decides. B (non-owner) edits, is denied, then
    undoes the edit byte-for-byte WITH its mtime. Meanwhile the owner pushed.
    The remote identity moved, so the box is needed and the push arrives."""
    run(claim_box(config_path=fleet["cpA"], box_index_name=fleet["idx"], verbose=False))
    run(sync_box(config_path=fleet["cpB"], box_index_name=fleet["idx"], verbose=False))
    assert verdict(fleet["cpB"], fleet["remote_root"]).skippable == [fleet["idx"]]

    notes = fleet["dataB"] / "notes.md"
    before = notes.stat()
    notes.write_text("edited on b\n")
    denied = run(sync_box(config_path=fleet["cpB"], box_index_name=fleet["idx"], verbose=False))
    assert denied[BoxPart.DATA][0].sync_condition is SyncCondition.WRITE_DENIED

    (fleet["dataA"] / "other.md").write_text("from the owner\n")
    run(sync_box(config_path=fleet["cpA"], box_index_name=fleet["idx"], verbose=False))

    notes.write_text("first\n")
    os.utime(notes, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert verdict(fleet["cpB"], fleet["remote_root"]).reasons.get(fleet["idx"]) == "data"
    result = _multi_sync(fleet["cpB"], "--skip-unchanged", "--print-skipped")
    assert fleet["idx"] in result.output
    assert (fleet["dataB"] / "other.md").read_text() == "from the owner\n"


def test_a_denied_verdict_writes_no_agreement(fleet):
    """WRITE_DENIED is not an agreement: the sidecar must keep naming the
    record B last agreed with, not the one it was refused against."""
    run(claim_box(config_path=fleet["cpA"], box_index_name=fleet["idx"], verbose=False))
    run(sync_box(config_path=fleet["cpB"], box_index_name=fleet["idx"], verbose=False))
    cfgB = get_config(fleet["cpB"])
    rec_path = get_boxyard_meta(cfgB).by_index_name[fleet["idx"]].get_local_sync_record_path(cfgB, BoxPart.DATA)
    agreed = read_remote_identity(rec_path)

    (fleet["dataA"] / "other.md").write_text("from the owner\n")
    run(sync_box(config_path=fleet["cpA"], box_index_name=fleet["idx"], verbose=False))
    (fleet["dataB"] / "notes.md").write_text("edited on b\n")
    res = run(sync_box(config_path=fleet["cpB"], box_index_name=fleet["idx"], verbose=False))
    assert res[BoxPart.DATA][0].sync_condition in (SyncCondition.WRITE_DENIED, SyncCondition.CONFLICT)
    assert read_remote_identity(rec_path) == agreed


def test_an_edit_landing_after_a_clean_probe_is_not_blessed(fleet, monkeypatch):
    import boxyard.cmds._sync_box as sync_box_module

    run(claim_box(config_path=fleet["cpA"], box_index_name=fleet["idx"], verbose=False))
    cfgA = get_config(fleet["cpA"])
    confA = get_boxyard_meta(cfgA).by_index_name[fleet["idx"]].get_local_part_path(cfgA, BoxPart.CONF)
    confA.mkdir(parents=True, exist_ok=True)
    (confA / const.RCLONE_EXCLUDE_FILENAME).write_text("*.log\n")
    run(sync_box(config_path=fleet["cpA"], box_index_name=fleet["idx"], verbose=False))
    run(sync_box(config_path=fleet["cpB"], box_index_name=fleet["idx"], verbose=False))

    cfgB = get_config(fleet["cpB"])
    bmB = get_boxyard_meta(cfgB).by_index_name[fleet["idx"]]
    base_path_for(bmB.get_local_sync_record_path(cfgB, BoxPart.DATA)).unlink()
    (fleet["dataB"] / "scratch.log").write_text("excluded by conf\n")

    real_probe = sync_box_module.push_would_transfer

    async def _probe_then_edit(*args, **kwargs):
        would = await real_probe(*args, **kwargs)
        (fleet["dataB"] / "notes.md").write_text("edited after the probe\n")
        return would

    monkeypatch.setattr(sync_box_module, "push_would_transfer", _probe_then_edit)
    first = run(sync_box(config_path=fleet["cpB"], box_index_name=fleet["idx"], verbose=False))
    monkeypatch.setattr(sync_box_module, "push_would_transfer", real_probe)
    assert first[BoxPart.DATA][0].sync_condition is SyncCondition.SYNCED

    assert verdict(fleet["cpB"], fleet["remote_root"]).reasons.get(fleet["idx"]) == "data"
    second = run(sync_box(config_path=fleet["cpB"], box_index_name=fleet["idx"], verbose=False))
    assert second[BoxPart.DATA][0].sync_condition is SyncCondition.WRITE_DENIED


# %% [markdown]
# ## Restic DATA: the pointer-stamp filter, behind the placement gate

# %%
#|export
def pointer_entry(yard):
    """(ModTime, Size) of the box's pointer, as the bulk listing would report."""
    config = get_config(yard["config_path"])
    out = subprocess.run(
        [
            get_rclone_binary(), "lsjson", "--config", str(config.rclone_config_path),
            "--files-only", "--recursive", "--max-depth", "2",
            "--filter", f"+ /*/{const.BOX_SNAPSHOT_POINTER_REL_PATH}",
            "--filter", "- **",
            f"{yard['remote_name']}:"
            f"{config.storage_locations[yard['remote_name']].store_path}/"
            f"{const.REMOTE_BOXES_REL_PATH}",
        ],
        capture_output=True, text=True,
    )
    result = {}
    for e in json.loads(out.stdout or "[]"):
        result[Path(e["Path"]).parts[0]] = (e.get("ModTime"), e.get("Size"))
    return result


def _stamp_pointer(yard):
    config = get_config(yard["config_path"])
    listing = pointer_entry(yard)
    modtime, size = listing[yard["idx"]]
    write_check_record(config, yard["idx"], BoxPart.DATA, 1000.0,
                       remote_modtime=modtime, remote_size=size)
    return listing


def test_a_plain_box_handed_to_the_restic_filter_is_always_needed(yard):
    from boxyard._restic import write_state

    config = get_config(yard["config_path"])
    listing = {yard["idx"]: ("2026-01-01T00:00:00Z", 42)}
    write_check_record(config, yard["idx"], BoxPart.DATA, 1000.0,
                       remote_modtime="2026-01-01T00:00:00Z", remote_size=42)
    write_state(config.boxyard_data_path, yard["idx"], "deadbeef",
                now_unix=4102444800.0, files=1)
    assert box_meta(yard).storage_format is StorageFormat.PLAIN

    needed, skippable = data_boxes_needing_sync(config, metas(yard), listing)
    assert skippable == []
    assert yard["idx"] in needed


@needs_restic
def test_a_converted_unchanged_box_is_skippable(yard):
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    listing = _stamp_pointer(yard)
    needed, skippable = data_boxes_needing_sync(config, metas(yard), listing)
    assert skippable == [yard["idx"]]
    assert needed == []
    # ...and through the full-pass verdict, which delegates restic DATA here.
    v = verdict(yard["config_path"], yard["remote_root"], pointer_override=listing)
    assert v.skippable == [yard["idx"]], v.reasons


@needs_restic
def test_a_relocating_restic_box_is_needed(yard):
    """The placement gate applies before the storage-format dispatch: a
    RELOCATING restic box with an untouched tree and a settled pointer was
    dropped while the real path raises 'run boxyard relocate'."""
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    listing = _stamp_pointer(yard)
    assert verdict(yard["config_path"], yard["remote_root"], pointer_override=listing).skippable == [yard["idx"]]
    _make_relocating(yard)
    v = verdict(yard["config_path"], yard["remote_root"], pointer_override=listing)
    assert v.reasons.get(yard["idx"]) == "data"


@needs_restic
def test_an_excluded_restic_box_is_provable(yard):
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    run(exclude_box(config_path=yard["config_path"], box_index_name=yard["idx"]))
    assert verdict(yard["config_path"], yard["remote_root"]).skippable == [yard["idx"]]


@needs_restic
def test_a_moved_pointer_is_never_skipped(yard):
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    listing = _stamp_pointer(yard)
    moved = {yard["idx"]: ("2099-01-01T00:00:00Z", listing[yard["idx"]][1])}
    needed, skippable = data_boxes_needing_sync(config, metas(yard), moved)
    assert needed == [yard["idx"]]
    assert skippable == []


@needs_restic
def test_a_locally_modified_restic_box_is_never_skipped(yard):
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    listing = _stamp_pointer(yard)
    (yard["data"] / "notes.md").write_text("edited after the last sync\n")
    needed, _ = data_boxes_needing_sync(config, metas(yard), listing)
    assert needed == [yard["idx"]]


@needs_restic
def test_a_lone_deletion_in_a_restic_box_is_never_skipped(yard):
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    listing = _stamp_pointer(yard)
    (yard["data"] / "notes.md").unlink()  # the ONLY change: no mtime survives it
    needed, skippable = data_boxes_needing_sync(config, metas(yard), listing)
    assert needed == [yard["idx"]]
    assert skippable == []


@needs_restic
def test_a_restic_box_with_no_check_record_is_never_skipped(yard):
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    needed, _ = data_boxes_needing_sync(config, metas(yard), pointer_entry(yard))
    assert needed == [yard["idx"]]


@needs_restic
def test_an_interrupted_restore_is_never_skipped(yard):
    from boxyard._restic import mark_pull_started

    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    listing = _stamp_pointer(yard)
    mark_pull_started(config.boxyard_data_path, yard["idx"], "0" * 64)
    needed, _ = data_boxes_needing_sync(config, metas(yard), listing)
    assert needed == [yard["idx"]]


@needs_restic
def test_a_restic_box_missing_from_the_listing_is_never_skipped(yard):
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    write_check_record(config, yard["idx"], BoxPart.DATA, 1000.0,
                       remote_modtime="T", remote_size=1)
    needed, _ = data_boxes_needing_sync(config, metas(yard), {})
    assert needed == [yard["idx"]]


@needs_restic
def test_the_real_filter_admits_the_pointer(yard):
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    first = _multi_sync(yard["config_path"], "-c", "data", "--skip-unchanged", "--print-skipped")
    assert yard["idx"] in first.output, "the first pass must sync the box and stamp it"
    second = _multi_sync(yard["config_path"], "-c", "data", "--skip-unchanged", "--print-skipped")
    assert yard["idx"] not in second.output


@needs_restic
def test_a_pointer_moved_after_the_box_was_synced_is_needed_next_pass(yard, monkeypatch):
    """The stamp is taken from the PRE-pass listing; a pass-end listing adopts
    a foreign push that lands after the box's own sync."""
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    _multi_sync(yard["config_path"], "--skip-unchanged")
    modify_boxmeta(config_path=yard["config_path"], box_index_name=yard["idx"],
                   modifications={"groups": ["nudge"]})
    pointer = _store(yard["remote_root"]) / const.REMOTE_BOXES_REL_PATH / yard["idx"] / const.BOX_SNAPSHOT_POINTER_REL_PATH

    real = cmds_module.sync_box

    async def _then_foreign_push(**kwargs):
        result = await real(**kwargs)
        pointer.write_text(pointer.read_text() + "\n")
        os.utime(pointer, (4102444800, 4102444800))
        return result

    monkeypatch.setattr(cmds_module, "sync_box", _then_foreign_push)
    _multi_sync(yard["config_path"], "--skip-unchanged")
    monkeypatch.setattr(cmds_module, "sync_box", real)

    nxt = _multi_sync(yard["config_path"], "--skip-unchanged", "--print-skipped")
    assert yard["idx"] in nxt.output, "the moved pointer was adopted as agreed"


@needs_restic
def test_convert_removes_the_data_record_and_its_sidecar(yard):
    rec_path = local_record_path(yard, BoxPart.DATA)
    assert read_remote_identity(rec_path) is not None
    run(convert_box(config_path=yard["config_path"], box_index_name=yard["idx"], verbose=False))
    assert not (remote_record_dir(yard["remote_root"], yard["idx"]) / "data.rec").exists()
    assert read_remote_identity(rec_path) is None

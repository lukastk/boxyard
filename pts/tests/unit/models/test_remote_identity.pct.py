# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Remote record identity
#
# The remote half of the full-pass skip is the md5 of each `<part>.rec` from
# one bulk `lsjson --hash`; the local half is the sidecar `_remote_identity`
# writes when the real path last agreed with that record. These tests pin:
#
# - the md5 this machine remembers is the md5 the listing will report (bytes
#   as written, bytes as read, and rclone's own hash all agree);
# - a sidecar is written only on an agreement (SYNCED / EXCLUDED / a completed
#   transfer), never on any other verdict, and degrades to "unknown" when
#   absent, torn or from another version;
# - the listings are projected exactly (depth, filename, hash presence,
#   directory entries) and keyed by store AND index name;
# - record writes leak no file descriptors (the bootstrap that made that
#   dangerous is gone, but a leak per record write was never acceptable).

# %%
#|default_exp unit.models.test_remote_identity

# %%
#|export
import asyncio
import hashlib
import os
from pathlib import Path

import pytest
from ulid import ULID

from boxyard import const
from boxyard._enums import BoxPart
from boxyard._models import SyncCondition, SyncRecord, SyncStatus, get_sync_status
from boxyard._remote_identity import (
    clear_inflight_push,
    clear_remote_identity,
    inflight_push_path,
    read_inflight_push,
    write_inflight_push,
    note_agreement,
    read_remote_identity,
    record_md5,
    remote_identity_path,
    write_remote_identity,
)
from boxyard._sync_policy import (
    RemoteBoxView,
    closure_of,
    project_box_listing,
    project_record_listing,
    remote_view_for,
)
from boxyard._utils import rclone_lsjson


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def alias_remote(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    conf = tmp_path / "rclone.conf"
    conf.write_text(f"[rem]\ntype = alias\nremote = {root}\n")
    return conf, root

# %% [markdown]
# ## The md5 is the same on every side

# %%
#|export
def test_written_read_and_listed_md5_agree(alias_remote):
    """
    `rclone_save` returns the md5 of what it wrote; `get_sync_status` hashes
    the text it read back; `rclone lsjson --hash` reports the file's md5.
    All three must be the same string, or the sidecar could never match.
    """
    conf, root = alias_remote
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    written = run(rec.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))

    on_disk = hashlib.md5((root / "sync_records" / "box" / "data.rec").read_bytes()).hexdigest()
    assert written == on_disk

    parsed, text = run(SyncRecord.rclone_read_raw(str(conf), "rem", "sync_records/box/data.rec"))
    assert parsed.ulid == rec.ulid
    assert record_md5(text) == on_disk

    listing = run(rclone_lsjson(
        str(conf), source="rem", source_path="sync_records", files_only=True,
        recursive=True, max_depth=2, filter=["+ /*/*.rec", "- **"], md5=True,
    ))
    assert listing[0]["Hashes"]["md5"] == on_disk


def test_a_record_written_by_another_serializer_still_hashes_as_read(alias_remote):
    """
    The sidecar hashes the remote's BYTES, not a re-serialization: a record
    written with different JSON formatting (an older boxyard, a hand edit)
    parses the same but must be remembered by the bytes the listing hashes.
    """
    conf, root = alias_remote
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    pretty = rec.model_dump_json(indent=2) + "\n"
    (root / "sync_records" / "box").mkdir(parents=True)
    (root / "sync_records" / "box" / "data.rec").write_text(pretty)

    parsed, text = run(SyncRecord.rclone_read_raw(str(conf), "rem", "sync_records/box/data.rec"))
    assert parsed.ulid == rec.ulid
    assert text == pretty
    assert record_md5(text) == hashlib.md5(pretty.encode()).hexdigest()
    assert record_md5(text) != record_md5(rec.serialized())

# %% [markdown]
# ## The sidecar

# %%
#|export
def _status(condition, rec, md5, **kw):
    return SyncStatus(
        sync_condition=condition, local_path_exists=True, remote_path_exists=True,
        local_sync_record=rec, remote_sync_record=rec, is_dir=True,
        remote_sync_record_md5=md5, **kw,
    )


def test_note_agreement_writes_only_on_synced_or_excluded(tmp_path):
    rec_path = tmp_path / "sync_records" / "box" / "data.rec"
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    md5 = record_md5(rec.serialized())

    for cond in SyncCondition:
        clear_remote_identity(rec_path)
        wrote = note_agreement(rec_path, _status(cond, rec, md5))
        expected = cond in (SyncCondition.SYNCED, SyncCondition.EXCLUDED)
        assert wrote is expected, cond
        assert (read_remote_identity(rec_path) is not None) is expected, cond

    assert note_agreement(rec_path, _status(SyncCondition.SYNCED, rec, md5)) is True
    ident = read_remote_identity(rec_path)
    assert ident == {"version": 1, "md5": md5, "ulid": str(rec.ulid), "sync_complete": True}
    assert remote_identity_path(rec_path) == rec_path.parent / f"data{const.BOX_REMOTE_IDENTITY_SUFFIX}"


def test_note_agreement_needs_a_remote_record_and_its_md5(tmp_path):
    rec_path = tmp_path / "data.rec"
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    assert note_agreement(rec_path, _status(SyncCondition.SYNCED, None, None)) is False
    assert note_agreement(rec_path, _status(SyncCondition.SYNCED, rec, None)) is False
    assert read_remote_identity(rec_path) is None


def test_an_incomplete_remote_record_is_remembered_as_incomplete(tmp_path):
    """A sidecar records what the bytes SAID; the filter refuses one that is
    not complete, so a remembered in-flight record can never prove anything."""
    rec_path = tmp_path / "data.rec"
    rec = SyncRecord.create(sync_complete=False, syncer_hostname="m")
    note_agreement(rec_path, _status(SyncCondition.SYNCED, rec, record_md5(rec.serialized())))
    assert read_remote_identity(rec_path)["sync_complete"] is False


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{",
        "[]",
        '{"version": 2, "md5": "x", "ulid": "y", "sync_complete": true}',
        '{"version": 1, "ulid": "y", "sync_complete": true}',
        '{"version": 1, "md5": 5, "ulid": "y", "sync_complete": true}',
        '{"version": 1, "md5": "x", "ulid": "y", "sync_complete": "yes"}',
    ],
)
def test_anything_but_a_well_formed_sidecar_reads_as_unknown(tmp_path, content):
    rec_path = tmp_path / "data.rec"
    remote_identity_path(rec_path).write_text(content)
    assert read_remote_identity(rec_path) is None


def test_write_is_atomic_and_clear_is_idempotent(tmp_path):
    rec_path = tmp_path / "sync_records" / "box" / "meta.rec"
    p = write_remote_identity(rec_path, md5="a" * 32, ulid=str(ULID()), sync_complete=True)
    assert p.exists() and not p.with_name(p.name + ".tmp").exists()
    clear_remote_identity(rec_path)
    clear_remote_identity(rec_path)
    assert read_remote_identity(rec_path) is None


def test_get_sync_status_reports_the_remote_md5(alias_remote, tmp_path):
    conf, root = alias_remote
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    md5 = run(rec.rclone_save(str(conf), "rem", "data.rec"))
    (root / "data").mkdir()
    local = tmp_path / "local"
    local.mkdir()
    status = run(get_sync_status(
        rclone_config_path=str(conf), local_path=str(local),
        local_sync_record_path=str(tmp_path / "local.rec"), remote="rem",
        remote_path="data", remote_sync_record_path="data.rec",
    ))
    assert status.remote_sync_record_md5 == md5

    absent = run(get_sync_status(
        rclone_config_path=str(conf), local_path=str(local),
        local_sync_record_path=str(tmp_path / "local.rec"), remote="rem",
        remote_path="nothing", remote_sync_record_path="nothing.rec",
    ))
    assert absent.remote_sync_record_md5 is None

# %% [markdown]
# ## Projecting the two listings

# %%
#|export
def _entries(*items):
    out = []
    for item in items:
        path, *rest = item if isinstance(item, tuple) else (item,)
        e = {"Path": path, "IsDir": False}
        for extra in rest:
            e.update(extra)
        out.append(e)
    return out


def test_records_are_keyed_by_store_and_box_with_their_md5():
    view = project_record_listing("sl", _entries(
        ("boxA/meta.rec", {"Hashes": {"md5": "m1"}}),
        ("boxA/data.rec", {"Hashes": {"md5": "d1"}}),
        ("boxB/meta.rec", {"Hashes": {"md5": "m2"}}),
    ))
    assert view == {("sl", "boxA"): {"meta": "m1", "data": "d1"}, ("sl", "boxB"): {"meta": "m2"}}


def test_an_unhashed_record_is_listed_with_no_identity():
    """The backend could not hash it: present, identity UNKNOWN -- never absent."""
    view = project_record_listing("sl", _entries(
        "boxA/meta.rec",
        ("boxA/data.rec", {"Hashes": {}}),
        ("boxA/conf.rec", {"Hashes": {"md5": ""}}),
    ))
    assert view == {("sl", "boxA"): {"meta": None, "data": None, "conf": None}}


def test_residue_sidecars_and_other_depths_are_not_records():
    view = project_record_listing("sl", _entries(
        ("boxA/data.rec.a8bc93c2.partial", {"Hashes": {"md5": "x"}}),
        ("boxA/data.base.json", {"Hashes": {"md5": "x"}}),
        ("boxA/data.remote.json", {"Hashes": {"md5": "x"}}),
        ("data.rec", {"Hashes": {"md5": "x"}}),
        ("boxA/sub/data.rec", {"Hashes": {"md5": "x"}}),
    ))
    assert view == {}


def test_the_box_listing_sees_every_entry_and_its_kind():
    view = project_box_listing("sl", _entries(
        ("boxA", {"IsDir": True}),
        "boxA/boxmeta.toml",
        ("boxA/conf", {"IsDir": True}),
        ("boxA/data", {"IsDir": True}),
        ("boxB", {"IsDir": True}),
        "boxB/boxmeta.toml",
        ("boxB/data.snapshot", {"ModTime": "T", "Size": 9}),
        ("boxB/data.restic", {"IsDir": True}),
        ("boxC", {"IsDir": True}),
        "boxC/conf",  # a FILE named conf: the wrong kind, never "absent"
        "boxC/data",
    ))
    assert view[("sl", "boxA")] == RemoteBoxView("boxA", boxmeta=True, conf_dir=True, data_dir=True)
    assert view[("sl", "boxB")] == RemoteBoxView("boxB", boxmeta=True, restic_dir=True, pointer=True)
    assert view[("sl", "boxC")] == RemoteBoxView("boxC", anomalies=["conf is a file", "data is a file"])


def test_two_stores_never_share_a_key():
    a = project_record_listing("store-a", _entries(("box/meta.rec", {"Hashes": {"md5": "m"}})))
    b = project_record_listing("store-b", _entries(("box/meta.rec", {"Hashes": {"md5": "m"}})))
    assert set(a) == {("store-a", "box")} and set(b) == {("store-b", "box")}


def test_the_real_box_listing_reports_empty_and_nested_only_conf_dirs(tmp_path):
    """rclone, with multi-sync's exact flags: the conf DIRECTORY entry appears
    whether it is empty or holds only a nested tree, and `data/` is never
    descended into."""
    root = tmp_path / "store"
    for box in ("empty", "nested", "none"):
        (root / "boxes" / box).mkdir(parents=True)
        (root / "boxes" / box / const.BOX_METAFILE_REL_PATH).write_text('name="x"\n')
    (root / "boxes" / "empty" / "conf").mkdir()
    (root / "boxes" / "nested" / "conf" / "deep").mkdir(parents=True)
    (root / "boxes" / "nested" / "conf" / "deep" / "settings.txt").write_text("s\n")
    (root / "boxes" / "none" / "data" / "sub").mkdir(parents=True)
    (root / "boxes" / "none" / "data" / "sub" / "x.txt").write_text("x\n")
    conf = tmp_path / "rclone.conf"
    conf.write_text(f"[loc]\ntype = alias\nremote = {root}\n")

    entries = run(rclone_lsjson(
        str(conf), source="loc", source_path="boxes", recursive=True, max_depth=2,
        filter=[
            f"+ /*/{const.BOX_METAFILE_REL_PATH}",
            f"+ /*/{const.BOX_SNAPSHOT_POINTER_REL_PATH}",
            f"+ /*/{const.BOX_CONF_REL_PATH}/",
            "- **",
        ],
    ))
    view = project_box_listing("loc", entries)
    assert view[("loc", "empty")].conf_dir is True
    assert view[("loc", "nested")].conf_dir is True
    assert view[("loc", "none")].conf_dir is False
    assert all(v.boxmeta for v in view.values())
    assert not any("x.txt" in e["Path"] for e in entries)


def test_a_box_is_matched_to_its_listed_directory_by_id():
    """Renamed elsewhere: found under the new name. Two directories with the
    id, or none with a boxmeta: not matched."""
    boxes = project_box_listing("sl", _entries(
        "20260101_aaaaa__new-name/boxmeta.toml",
        "20260102_bbbbb__one/boxmeta.toml",
        "20260102_bbbbb__two/boxmeta.toml",
        ("20260103_ccccc__no-boxmeta/conf", {"IsDir": True}),
        "not-an-index-name/boxmeta.toml",
    ))
    assert remote_view_for(boxes, "sl", "20260101_aaaaa").index_name == "20260101_aaaaa__new-name"
    assert remote_view_for(boxes, "sl", "20260102_bbbbb") is None
    assert remote_view_for(boxes, "sl", "20260103_ccccc") is None
    assert remote_view_for(boxes, "other-store", "20260101_aaaaa") is None
    assert remote_view_for(boxes, "sl", "20260104_ddddd") is None


def test_data_drags_meta_and_conf_into_the_closure():
    assert closure_of([BoxPart.DATA]) == {BoxPart.META, BoxPart.CONF, BoxPart.DATA}
    assert closure_of([BoxPart.META]) == {BoxPart.META}
    assert closure_of([BoxPart.CONF]) == {BoxPart.CONF}
    assert closure_of(list(BoxPart)) == set(BoxPart)

# %% [markdown]
# ## Record writes

# %%
#|export
def test_a_failed_record_write_raises(alias_remote, monkeypatch):
    import boxyard._utils as utils_module

    conf, _root = alias_remote

    async def _failing(**kwargs):
        return False, "", "sftp: connection limit"

    monkeypatch.setattr(utils_module, "rclone_copyto", _failing)
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    with pytest.raises(RuntimeError, match="connection limit"):
        run(rec.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc")
def test_record_writes_do_not_leak_descriptors(alias_remote):
    """`mkstemp` hands back an OPEN descriptor; discarding it leaked one per
    record write (measured by the implementation review: ten writes, ten fds)."""
    conf, _root = alias_remote
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    run(rec.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))  # warm up
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(10):
        run(rec.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))
    after = len(os.listdir("/proc/self/fd"))
    assert after <= before, f"leaked {after - before} descriptors over ten writes"


def test_a_remote_record_write_touches_nothing_else(alias_remote):
    """No markers, no sweeps: the record is the only file a save produces."""
    conf, root = alias_remote
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    run(rec.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))
    run(rec.rclone_save(str(conf), "rem", "sync_records/box/meta.rec"))
    assert {p.name for p in (root / "sync_records" / "box").iterdir()} == {"data.rec", "meta.rec"}


# %% [markdown]
# ## The in-flight push sidecar

# %%
#|export
def test_the_inflight_sidecar_round_trips_and_degrades_to_none(tmp_path):
    rec_path = tmp_path / "sync_records" / "box" / "data.rec"
    assert read_inflight_push(rec_path) is None
    u = str(ULID())
    write_inflight_push(rec_path, ulid=u)
    assert inflight_push_path(rec_path) == rec_path.parent / f"data{const.BOX_INFLIGHT_PUSH_SUFFIX}"
    assert read_inflight_push(rec_path) == u
    inflight_push_path(rec_path).write_text("{")
    assert read_inflight_push(rec_path) is None
    inflight_push_path(rec_path).write_text('{"version": 2, "ulid": "x"}')
    assert read_inflight_push(rec_path) is None
    clear_inflight_push(rec_path)
    clear_inflight_push(rec_path)
    assert read_inflight_push(rec_path) is None

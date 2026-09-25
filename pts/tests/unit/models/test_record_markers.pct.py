# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Sync-record generation markers
#
# A remote sync record publishes its identity as a zero-byte sibling,
# `<part>.rec.<ULID>`, so one bulk listing of `sync_records/` names every
# part's CURRENT record without a timestamp or a size in sight. Everything the
# full-pass skip filter trusts about the remote rests on three properties:
#
# - **Parsing is strict.** Only `<part>.rec.<26 Crockford chars>` is a marker.
#   rclone's `<name>.<hash>.partial` upload residue (present on the live remote),
#   the record itself, and the local `.base.json` sidecar all fall through.
# - **Exactly one marker means "known".** Two markers (two unowned pushers
#   interleaving) or none read as identity UNKNOWN, never as the older identity.
# - **Every remote record write sweeps, writes, publishes -- in that order.**
#   `rclone_save` to a non-local destination retires the previous marker before
#   the record is written and publishes the new one after, so no crash leaves a
#   marker beside a record it does not describe; the sweep touches nothing
#   else -- not the record, not another part's marker.

# %%
#|default_exp unit.models.test_record_markers

# %%
#|export
import asyncio
from pathlib import Path

import pytest
from ulid import ULID

from boxyard._enums import BoxPart
from boxyard._models import (
    SyncRecord,
    parse_record_marker,
    record_marker_name,
    write_record_marker,
)
from boxyard._sync_policy import RemoteRecordView, closure_of, project_record_listing


def run(coro):
    return asyncio.run(coro)


U1 = str(ULID())
U2 = str(ULID())

# %% [markdown]
# ## Parsing

# %%
#|export
def test_a_well_formed_marker_parses_to_part_and_ulid():
    assert parse_record_marker(f"data.rec.{U1}") == ("data", U1)
    assert parse_record_marker(f"meta.rec.{U1}") == ("meta", U1)
    assert parse_record_marker(f"conf.rec.{U1}") == ("conf", U1)
    assert record_marker_name("data.rec", U1) == f"data.rec.{U1}"


@pytest.mark.parametrize(
    "name",
    [
        "data.rec",  # the record itself
        "data.rec.a8bc93c2.partial",  # rclone upload residue, seen on the live remote
        f"data.rec.{U1}.partial",  # residue of a marker upload
        "data.base.json",  # the local fingerprint sidecar
        f"data.rec.{U1.lower()}",  # ULIDs are upper-case Crockford; lower is not one
        f"data.rec.{U1[:-1]}",  # 25 characters
        f"data.rec.{U1}0",  # 27 characters
        f"data.rec.{U1[:-1]}I",  # I is not in the Crockford alphabet
        f"data.{U1}",  # no `.rec`
        f"data.rec.{U1}.json",
        "",
    ],
)
def test_anything_else_is_not_a_marker(name):
    assert parse_record_marker(name) is None, name

# %% [markdown]
# ## Projecting a listing

# %%
#|export
def _entries(*paths):
    return [{"Path": p} for p in paths]


def test_records_and_markers_are_keyed_per_box():
    view = project_record_listing(
        _entries(
            "boxA/meta.rec",
            f"boxA/meta.rec.{U1}",
            "boxA/data.rec",
            f"boxA/data.rec.{U2}",
            "boxB/meta.rec",
        )
    )
    assert set(view) == {"boxA", "boxB"}
    assert view["boxA"].records == {"meta", "data"}
    assert view["boxA"].identity(BoxPart.META) == U1
    assert view["boxA"].identity(BoxPart.DATA) == U2
    assert view["boxA"].identity(BoxPart.CONF) is None
    assert view["boxB"].records == {"meta"}
    assert view["boxB"].identity(BoxPart.META) is None


def test_two_markers_for_one_part_read_as_unknown():
    """Two unowned pushers interleaving can leave both; neither may be trusted."""
    view = project_record_listing(
        _entries("boxA/data.rec", f"boxA/data.rec.{U1}", f"boxA/data.rec.{U2}")
    )
    assert sorted(view["boxA"].markers["data"]) == sorted([U1, U2])
    assert view["boxA"].identity(BoxPart.DATA) is None


def test_upload_residue_and_sidecars_are_not_keyed():
    view = project_record_listing(
        _entries(
            "boxA/data.rec.a8bc93c2.partial",
            "boxA/data.base.json",
            "boxA/data.rec.json",
            "boxA/notes.txt",
        )
    )
    assert view == {}, view


def test_only_depth_two_paths_count():
    view = project_record_listing(
        _entries(
            "data.rec",  # depth 1
            f"boxA/sub/data.rec.{U1}",  # depth 3
            f"boxA/data.rec.{U1}",
        )
    )
    assert set(view) == {"boxA"}
    assert view["boxA"].records == set()
    assert view["boxA"].identity(BoxPart.DATA) == U1


def test_an_empty_or_absent_listing_projects_to_nothing():
    assert project_record_listing(None) == {}
    assert project_record_listing([]) == {}
    assert RemoteRecordView().identity(BoxPart.META) is None

# %% [markdown]
# ## The dependency closure

# %%
#|export
def test_data_drags_meta_and_conf_into_the_closure():
    assert closure_of([BoxPart.DATA]) == {BoxPart.META, BoxPart.CONF, BoxPart.DATA}
    assert closure_of([BoxPart.META]) == {BoxPart.META}
    assert closure_of([BoxPart.CONF]) == {BoxPart.CONF}
    assert closure_of([BoxPart.META, BoxPart.CONF]) == {BoxPart.META, BoxPart.CONF}
    assert closure_of(list(BoxPart)) == set(BoxPart)

# %% [markdown]
# ## Writing: sweep, record, marker -- and nothing else

# %%
#|export
@pytest.fixture
def alias_remote(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    conf = tmp_path / "rclone.conf"
    conf.write_text(f"[rem]\ntype = alias\nremote = {root}\n")
    return conf, root


def _names(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()} if directory.is_dir() else set()


def test_a_remote_record_save_publishes_its_marker(alias_remote):
    conf, root = alias_remote
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    run(rec.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))

    assert _names(root / "sync_records" / "box") == {
        "data.rec",
        f"data.rec.{rec.ulid}",
    }
    assert (root / "sync_records" / "box" / f"data.rec.{rec.ulid}").stat().st_size == 0


def test_a_local_record_save_publishes_nothing(alias_remote, tmp_path):
    """Markers are a REMOTE listing's business; the local record has none."""
    conf, _root = alias_remote
    local = tmp_path / "local_records" / "data.rec"
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    run(rec.rclone_save(str(conf), "", str(local)))
    assert _names(local.parent) == {"data.rec"}


def test_a_second_save_retires_the_previous_marker_only(alias_remote):
    """
    The sweep must remove the OLD marker of THIS part and leave everything
    else: the record, the other part's marker, and the other part's record.
    """
    conf, root = alias_remote
    first = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    run(first.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))
    meta = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    run(meta.rclone_save(str(conf), "rem", "sync_records/box/meta.rec"))

    second = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    run(second.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))

    assert _names(root / "sync_records" / "box") == {
        "data.rec",
        f"data.rec.{second.ulid}",
        "meta.rec",
        f"meta.rec.{meta.ulid}",
    }
    saved = SyncRecord.model_validate_json(
        (root / "sync_records" / "box" / "data.rec").read_text()
    )
    assert saved.ulid == second.ulid


def test_the_sweep_is_scoped_to_the_record_directory(alias_remote):
    """A marker for the same part in ANOTHER box's directory is not touched."""
    conf, root = alias_remote
    other = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    run(other.rclone_save(str(conf), "rem", "sync_records/other/data.rec"))

    run(write_record_marker(str(conf), "rem", "sync_records/box/data.rec", U1))
    run(write_record_marker(str(conf), "rem", "sync_records/box/data.rec", U2))

    assert _names(root / "sync_records" / "box") == {f"data.rec.{U2}"}
    assert _names(root / "sync_records" / "other") == {
        "data.rec",
        f"data.rec.{other.ulid}",
    }


def test_a_failed_record_write_raises(alias_remote, monkeypatch):
    """
    `rclone_save` used to discard `rclone_copyto`'s result. Under a skip filter
    a silently failed remote record write is a permanent, invisible wedge; it
    was never acceptable before either.
    """
    import boxyard._utils as utils_module

    conf, _root = alias_remote

    async def _failing(**kwargs):
        return False, "", "sftp: connection limit"

    monkeypatch.setattr(utils_module, "rclone_copyto", _failing)
    rec = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    with pytest.raises(RuntimeError, match="connection limit"):
        run(rec.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))


def test_a_failed_marker_write_raises(alias_remote, monkeypatch):
    import boxyard._utils.rclone as rclone_module

    conf, _root = alias_remote

    async def _failing(**kwargs):
        return False, "", "disk full"

    monkeypatch.setattr(rclone_module, "rclone_copyto", _failing)
    with pytest.raises(RuntimeError, match="disk full"):
        run(write_record_marker(str(conf), "rem", "sync_records/box/data.rec", U1))

# %% [markdown]
# ## The order of writes is what makes a crash safe
#
# Sweep, record, marker. Every prefix of that sequence leaves either the old
# state or NO marker -- never a marker beside a record it does not describe.

# %%
#|export
def test_the_sweep_precedes_the_record_write(alias_remote, monkeypatch):
    """
    A crash between the record write and the marker write must leave NO
    marker. If the old marker survived it, a machine holding that identity
    would skip a box whose remote had just moved.
    """
    import boxyard._utils as utils_module

    conf, root = alias_remote
    first = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    run(first.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))
    assert _names(root / "sync_records" / "box") == {"data.rec", f"data.rec.{first.ulid}"}

    real = utils_module.rclone_copyto
    calls = []

    async def _record_write_then_crash(**kwargs):
        calls.append(kwargs["dest_path"])
        res = await real(**kwargs)
        if kwargs["dest_path"].endswith("data.rec"):
            raise ConnectionError("crashed right after the record write")
        return res

    monkeypatch.setattr(utils_module, "rclone_copyto", _record_write_then_crash)
    second = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    with pytest.raises(ConnectionError):
        run(second.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))

    assert calls == ["sync_records/box/data.rec"], "the marker was attempted before the crash"
    assert _names(root / "sync_records" / "box") == {"data.rec"}, (
        "a marker survived the crash window"
    )


def test_an_incomplete_record_publishes_its_own_marker(alias_remote):
    """
    The in-flight record a push writes first replaces the previous identity.
    Another machine then reads an identity it does not hold and pays the real
    check -- which is what reports an interrupted push if this one dies.
    """
    conf, root = alias_remote
    settled = SyncRecord.create(sync_complete=True, syncer_hostname="m")
    run(settled.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))
    in_flight = SyncRecord.create(sync_complete=False, syncer_hostname="m")
    run(in_flight.rclone_save(str(conf), "rem", "sync_records/box/data.rec"))
    assert _names(root / "sync_records" / "box") == {"data.rec", f"data.rec.{in_flight.ulid}"}

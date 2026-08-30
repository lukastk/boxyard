# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `--skip-unchanged` — one listing, both questions
#
# The DATA skip rides the SAME bulk `lsjson` the META skip already runs, because
# `boxes/<box>/data.snapshot` sits at depth 2 beside `boxmeta.toml`. So the DATA
# half costs **no additional remote calls at all**.
#
# Two things here are corrections rather than additions, and both are tested
# directly:
#
# 1. The listing is keyed by BOX AND FILENAME. rclone has no implicit exclude, so
#    the old `+ boxmeta.toml` filter already returned `data.snapshot` too, and
#    keying by box alone let one overwrite the other — silently disabling the
#    META skip for exactly the boxes a migration creates.
# 2. A box is skipped only if EVERY REQUESTED PART is provably unchanged.
#    `--skip-unchanged-meta` used to drop a box on META evidence alone, so a full
#    pass would skip a box whose DATA had changed locally.

# %%
#|default_exp integration.cmds.test_skip_unchanged

# %%
#|export
import asyncio
import shutil
from pathlib import Path

import pytest

from boxyard import const
from boxyard._enums import BoxPart, StorageFormat
from boxyard._models import BoxMeta, get_boxyard_meta
from boxyard._sync_policy import (
    check_record_path,
    data_boxes_needing_sync,
    meta_boxes_needing_sync,
    write_check_record,
)
from boxyard.cmds import convert_box, new_box, sync_box
from boxyard.config import get_config

pytestmark = pytest.mark.integration


def run(coro):
    return asyncio.run(coro)


needs_restic = pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic binary not available"
)


# %% [markdown]
# ## The listing returns both files, keyed separately
#
# The concrete answer to "what does the single listing return".

# %%
#|export
def test_one_listing_returns_both_files_at_depth_2(tmp_path):
    """
    rclone, exactly as multi-sync calls it. `data.snapshot` and `boxmeta.toml`
    are both depth-2 files under `boxes/<box>/`, so one call sees both, and
    `data/` contents at depth 3+ are excluded by `--max-depth 2`.
    """
    import subprocess

    from boxyard._utils import get_rclone_binary

    root = tmp_path / "store"
    for box, extra in (("boxA", None), ("boxB", "data.snapshot")):
        (root / "boxes" / box).mkdir(parents=True)
        (root / "boxes" / box / const.BOX_METAFILE_REL_PATH).write_text('name="x"\n')
        if extra:
            (root / "boxes" / box / extra).write_text('{"snapshot":"abc"}\n')
    (root / "boxes" / "boxA" / "data" / "sub").mkdir(parents=True)
    (root / "boxes" / "boxA" / "data" / "sub" / "deep.txt").write_text("deep\n")

    conf = tmp_path / "rclone.conf"
    conf.write_text(f"[loc]\ntype = alias\nremote = {root}\n")

    out = subprocess.run(
        [
            get_rclone_binary(), "lsjson", "--config", str(conf),
            "--files-only", "--recursive", "--max-depth", "2",
            "--filter", f"+ {const.BOX_METAFILE_REL_PATH}",
            "--filter", f"+ {const.BOX_SNAPSHOT_POINTER_REL_PATH}",
            "--filter", "- **",
            "loc:boxes",
        ],
        capture_output=True, text=True, check=True,
    )
    import json

    paths = sorted(e["Path"] for e in json.loads(out.stdout))
    assert paths == [
        "boxA/boxmeta.toml",
        "boxB/boxmeta.toml",
        "boxB/data.snapshot",
    ]
    assert not any("deep.txt" in p for p in paths), "depth-3 content leaked in"


def test_the_listing_must_be_keyed_by_box_and_filename():
    """
    The latent bug. Keying by box alone lets `data.snapshot` overwrite
    `boxmeta.toml`, so a converted box's META stamp is compared against the
    POINTER's ModTime and the box can never be skipped -- silently disabling the
    META optimisation for exactly the boxes a migration creates.
    """
    entries = [
        {"Path": "boxB/boxmeta.toml", "ModTime": "T1", "Size": 10},
        {"Path": "boxB/data.snapshot", "ModTime": "T2", "Size": 99},
    ]

    by_box_only = {}
    for e in entries:
        by_box_only[Path(e["Path"]).parts[0]] = (e["ModTime"], e["Size"])
    assert by_box_only["boxB"] == ("T2", 99), "the two collide, as they did"

    by_box_and_file = {}
    for e in entries:
        parts = Path(e["Path"]).parts
        by_box_and_file.setdefault(parts[0], {})[parts[1]] = (e["ModTime"], e["Size"])
    assert by_box_and_file["boxB"][const.BOX_METAFILE_REL_PATH] == ("T1", 10)
    assert by_box_and_file["boxB"][const.BOX_SNAPSHOT_POINTER_REL_PATH] == ("T2", 99)


# %% [markdown]
# ## The DATA skip filter

# %%
#|export
@pytest.fixture
def yard(temp_boxyard, monkeypatch, tmp_path):
    remote_name, remote_root, config, config_path, _dp = temp_boxyard
    monkeypatch.setenv("BOXYARD_RESTIC_PASSWORD", "skip-test-password")
    for target in ("boxyard.const", "boxyard._restic.const"):
        monkeypatch.setattr(f"{target}.RESTIC_CANONICAL_ROOT", str(tmp_path / "canon"))

    idx = new_box(config_path=config_path, box_name="skipbox",
                  storage_location=remote_name, claim=False)
    data = get_boxyard_meta(config).by_index_name[idx].get_local_part_path(
        config, BoxPart.DATA
    )
    (data / "notes.md").write_text("first\n")
    run(sync_box(config_path=config_path, box_index_name=idx, verbose=False))
    return {
        "idx": idx, "config_path": config_path, "remote_name": remote_name,
        "remote_root": remote_root, "data": data,
    }


def metas(yard):
    return get_boxyard_meta(get_config(yard["config_path"])).box_metas


def pointer_entry(yard):
    """(ModTime, Size) of the box's pointer, as the bulk listing would report."""
    import json
    import subprocess

    from boxyard._utils import get_rclone_binary

    config = get_config(yard["config_path"])
    out = subprocess.run(
        [
            get_rclone_binary(), "lsjson", "--config", str(config.rclone_config_path),
            "--files-only", "--recursive", "--max-depth", "2",
            "--filter", f"+ {const.BOX_SNAPSHOT_POINTER_REL_PATH}",
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


# %%
#|export
def test_a_plain_box_is_never_skippable_for_data(yard):
    """
    A plain box's DATA has no cheap remote signal -- which is the reason its
    no-op sync costs 30 s. It must always go through the real path.

    Everything else is arranged to say "skippable" -- a matching pointer entry,
    a matching check record, an unmodified tree, a local state record -- so the
    ONLY thing keeping this box out of `skippable` is the format guard. Without
    that, a passing test would prove nothing, which is what mutation testing
    caught the first time.
    """
    from boxyard._restic import write_state

    config = get_config(yard["config_path"])
    listing = {yard["idx"]: ("2026-01-01T00:00:00Z", 42)}
    write_check_record(config, yard["idx"], BoxPart.DATA, 1000.0,
                       remote_modtime="2026-01-01T00:00:00Z", remote_size=42)
    # A state record whose timestamp is far in the future, so the local tree
    # cannot look modified either.
    write_state(config.boxyard_data_path, yard["idx"], "deadbeef",
                now_unix=4102444800.0, files=1)

    assert BoxMeta.load(
        config, yard["remote_name"], yard["idx"]
    ).storage_format is StorageFormat.PLAIN

    needed, skippable = data_boxes_needing_sync(config, metas(yard), listing)
    assert skippable == [], "a plain box was skipped on evidence it cannot have"
    assert yard["idx"] in needed


@needs_restic
def test_a_converted_unchanged_box_is_skippable(yard):
    run(convert_box(config_path=yard["config_path"],
                    box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    listing = pointer_entry(yard)
    modtime, size = listing[yard["idx"]]
    write_check_record(config, yard["idx"], BoxPart.DATA, 1000.0,
                       remote_modtime=modtime, remote_size=size)

    needed, skippable = data_boxes_needing_sync(config, metas(yard), listing)
    assert skippable == [yard["idx"]]
    assert needed == []


@needs_restic
def test_a_moved_pointer_is_never_skipped(yard):
    run(convert_box(config_path=yard["config_path"],
                    box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    listing = pointer_entry(yard)
    modtime, size = listing[yard["idx"]]
    write_check_record(config, yard["idx"], BoxPart.DATA, 1000.0,
                       remote_modtime=modtime, remote_size=size)

    # As another machine's push would leave it.
    moved = {yard["idx"]: ("2099-01-01T00:00:00Z", size)}
    needed, skippable = data_boxes_needing_sync(config, metas(yard), moved)
    assert needed == [yard["idx"]]
    assert skippable == []


@needs_restic
def test_a_locally_modified_box_is_never_skipped(yard):
    run(convert_box(config_path=yard["config_path"],
                    box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    listing = pointer_entry(yard)
    modtime, size = listing[yard["idx"]]
    write_check_record(config, yard["idx"], BoxPart.DATA, 1000.0,
                       remote_modtime=modtime, remote_size=size)

    (yard["data"] / "notes.md").write_text("edited after the last sync\n")

    needed, skippable = data_boxes_needing_sync(config, metas(yard), listing)
    assert needed == [yard["idx"]]


@needs_restic
def test_a_box_with_no_check_record_is_never_skipped(yard):
    """Degrades in ONE direction: unknown means do the work."""
    run(convert_box(config_path=yard["config_path"],
                    box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    needed, skippable = data_boxes_needing_sync(
        config, metas(yard), pointer_entry(yard)
    )
    assert needed == [yard["idx"]]


@needs_restic
def test_an_interrupted_restore_is_never_skipped(yard):
    """
    A torn tree is exactly when the real path has work to do. Skipping it would
    leave the box half-restored until something else happened to notice.
    """
    from boxyard._restic import mark_pull_started

    run(convert_box(config_path=yard["config_path"],
                    box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    listing = pointer_entry(yard)
    modtime, size = listing[yard["idx"]]
    write_check_record(config, yard["idx"], BoxPart.DATA, 1000.0,
                       remote_modtime=modtime, remote_size=size)
    mark_pull_started(config.boxyard_data_path, yard["idx"], "0" * 64)

    needed, skippable = data_boxes_needing_sync(config, metas(yard), listing)
    assert needed == [yard["idx"]]


@needs_restic
def test_a_box_missing_from_the_listing_is_never_skipped(yard):
    run(convert_box(config_path=yard["config_path"],
                    box_index_name=yard["idx"], verbose=False))
    config = get_config(yard["config_path"])
    write_check_record(config, yard["idx"], BoxPart.DATA, 1000.0,
                       remote_modtime="T", remote_size=1)
    needed, skippable = data_boxes_needing_sync(config, metas(yard), {})
    assert needed == [yard["idx"]]


# %% [markdown]
# ## The correction: a box is skipped only if every requested part is provable

# %%
#|export
@needs_restic
def test_the_real_bulk_listing_keeps_the_two_files_apart(yard, monkeypatch):
    """
    The production path, not a simulation. A converted box has BOTH files at
    depth 2; if the listing keys by box alone, one overwrites the other, the
    META stamp is compared against the POINTER, and the box can never be
    skipped for META again.

    Driven through the real `multi-sync` so the fix is tested where it lives.
    """
    from typer.testing import CliRunner

    from boxyard._cli.app import app
    from boxyard._sync_policy import read_check_record

    def _multi_sync(*args):
        result = CliRunner().invoke(
            app,
            ["--config", str(yard["config_path"]), "multi-sync", *args],
        )
        assert result.exit_code == 0, f"exited {result.exit_code}\n{result.output}"
        return result

    run(convert_box(config_path=yard["config_path"],
                    box_index_name=yard["idx"], verbose=False))
    _multi_sync("-c", "meta")
    _multi_sync("-c", "meta", "--skip-unchanged-meta")

    config = get_config(yard["config_path"])
    meta_record = read_check_record(config, yard["idx"], BoxPart.META)
    assert meta_record is not None

    # The stamp must describe the BOXMETA, not the pointer. Compare it against
    # what each file actually reports.
    pointer = pointer_entry(yard).get(yard["idx"])
    assert pointer is not None, "precondition: the box is converted"
    assert (meta_record["remote_modtime"], meta_record["remote_size"]) != pointer, (
        "the META stamp was taken from data.snapshot -- the two collided"
    )

    # ...and with a correct stamp the box is skippable for META, which is the
    # optimisation the collision silently switched off.
    from boxyard._sync_policy import meta_boxes_needing_sync as _meta_needing

    import json as _json
    import subprocess as _sp

    from boxyard._utils import get_rclone_binary

    out = _sp.run(
        [
            get_rclone_binary(), "lsjson", "--config",
            str(config.rclone_config_path), "--files-only", "--recursive",
            "--max-depth", "2",
            "--filter", f"+ {const.BOX_METAFILE_REL_PATH}", "--filter", "- **",
            f"{yard['remote_name']}:"
            f"{config.storage_locations[yard['remote_name']].store_path}/"
            f"{const.REMOTE_BOXES_REL_PATH}",
        ],
        capture_output=True, text=True,
    )
    meta_listing = {
        Path(e["Path"]).parts[0]: (e.get("ModTime"), e.get("Size"))
        for e in _json.loads(out.stdout or "[]")
    }
    _, skippable = _meta_needing(config, metas(yard), meta_listing)
    assert yard["idx"] in skippable


def test_meta_evidence_alone_does_not_skip_a_data_sync(yard):
    """
    `--skip-unchanged-meta` used to drop a box from the pass on META evidence
    alone, so a full pass would skip a box whose DATA had changed locally: its
    boxmeta was settled, which says nothing about its files. Latent only because
    the flag has never been switched on.

    Expressed at the level the fix lives at -- the intersection over requested
    parts -- because DATA is not provable for a plain box at all.
    """
    config = get_config(yard["config_path"])
    box_metas = metas(yard)

    _, meta_skippable = meta_boxes_needing_sync(config, box_metas, {})
    _, data_skippable = data_boxes_needing_sync(config, box_metas, {})

    both_parts = set(meta_skippable) & set(data_skippable)
    assert both_parts == set(), "a plain box's DATA is never provable"

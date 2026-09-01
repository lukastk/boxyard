# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Depth-2 listings must only match what they say they match
#
# Two bulk `rclone lsjson` calls list `boxes/*/` at `--max-depth 2` with a `+`
# filter and no terminating `- **`:
#
# - `sync-missing-meta`, which runs on **every machine every 15 minutes**;
# - `multi-sync --skip-unchanged-meta`'s bulk listing.
#
# **rclone has no implicit exclude.** A filter list of only `+` rules includes
# everything that does not match, so both calls actually return *every* file at
# depth 2, and the `+ boxmeta.toml` reads as documentation rather than as a
# filter.
#
# Today that is harmless: `boxmeta.toml` is the only file at depth 2 in a box
# directory. It stops being harmless the moment anything else lands there — and
# something is about to, since restic-backed boxes keep a `data.snapshot`
# pointer beside `boxmeta.toml` precisely so this listing can see it.
#
# These tests use a stray file rather than that pointer, because the defect is
# about the filter and not about restic: any depth-2 file breaks it.

# %%
#|default_exp integration.cmds.test_depth2_listing_filters

# %%
#|export
import asyncio
from pathlib import Path

import pytest

from boxyard import const
from boxyard._enums import BoxPart
from boxyard._models import get_boxyard_meta
from boxyard.cmds import new_box, sync_box, sync_missing_boxmetas
from boxyard.config import get_config

pytestmark = pytest.mark.integration


def run(coro):
    return asyncio.run(coro)


# %%
#|export
@pytest.fixture
def two_machines():
    """
    Machine A owns a box; machine B has never seen it. B is the one that runs
    `sync-missing-meta`, which is the loop under test.
    """
    from tests.integration.conftest import create_boxyards

    remote_name, remote_root, yards = create_boxyards(num_boxyards=2)
    (cfgA, cpA, _), (cfgB, cpB, _) = yards

    idx = new_box(config_path=cpA, box_name="straybox",
                  storage_location=remote_name, claim=False)
    data = get_boxyard_meta(cfgA).by_index_name[idx].get_local_part_path(
        cfgA, BoxPart.DATA
    )
    (data / "notes.md").write_text("content\n")
    run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))

    return {
        "idx": idx, "cfgB": cfgB, "cpB": cpB, "remote_name": remote_name,
        "box_root": remote_root / "boxyard" / const.REMOTE_BOXES_REL_PATH / idx,
    }


def local_store_files(two_machines):
    root = (
        get_config(two_machines["cpB"]).local_store_path
        / two_machines["remote_name"]
        / two_machines["idx"]
    )
    if not root.is_dir():
        return set()
    return {p.name for p in root.iterdir() if p.is_file()}


# %% [markdown]
# ## The listing must not treat a stray depth-2 file as a boxmeta

# %%
#|export
def test_a_stray_depth2_file_is_not_reported_as_a_missing_boxmeta(two_machines):
    """
    The defect. Without a terminating `- **`, any file beside `boxmeta.toml`
    comes back from the listing and lands in `missing_metas`, so
    `sync-missing-meta` tries to fetch it as though it were a box's metadata.
    """
    (two_machines["box_root"] / "data.snapshot").write_text('{"snapshot":"abc"}\n')

    missing = run(
        sync_missing_boxmetas(config_path=two_machines["cpB"], verbose=False)
    )

    assert all(Path(p).name == const.BOX_METAFILE_REL_PATH for p in missing), (
        f"the listing returned non-boxmeta paths: {missing}"
    )


def test_a_stray_depth2_file_is_not_copied_into_the_local_store(two_machines):
    """
    The local store mirrors a box's META and CONF. A file that is neither has no
    business being pulled into it.
    """
    (two_machines["box_root"] / "data.snapshot").write_text('{"snapshot":"abc"}\n')

    run(sync_missing_boxmetas(config_path=two_machines["cpB"], verbose=False))

    assert "data.snapshot" not in local_store_files(two_machines)
    assert const.BOX_METAFILE_REL_PATH in local_store_files(two_machines)


def test_a_known_box_stops_being_reported_once_it_is_local(two_machines):
    """
    Idempotency, which holds either way.

    Note this passed on the BROKEN code too, and for a reason worth writing
    down: the stray file was pulled into the local store on the first pass, so
    the second pass saw it on both sides and the difference was empty. The harm
    was never an endless loop -- it was that the pollution is what ended it.
    """
    (two_machines["box_root"] / "data.snapshot").write_text('{"snapshot":"abc"}\n')

    first = run(sync_missing_boxmetas(config_path=two_machines["cpB"], verbose=False))
    assert first, "precondition: the box really was missing the first time"

    second = run(sync_missing_boxmetas(config_path=two_machines["cpB"], verbose=False))
    assert second == [], (
        f"the box is still reported as missing after being fetched: {second}"
    )


def test_the_ordinary_case_is_unaffected(two_machines):
    """The listing must still find a genuinely missing boxmeta."""
    missing = run(
        sync_missing_boxmetas(config_path=two_machines["cpB"], verbose=False)
    )
    assert missing == [f"{two_machines['idx']}/{const.BOX_METAFILE_REL_PATH}"]
    assert const.BOX_METAFILE_REL_PATH in local_store_files(two_machines)
    assert run(
        sync_missing_boxmetas(config_path=two_machines["cpB"], verbose=False)
    ) == []


# %% [markdown]
# ## The trap in the obvious fix
#
# Adding `- **` alone is not enough, and the natural way to "just add the
# exclude" is worse than the bug it fixes.

# %%
#|export
def test_anchoring_at_the_root_would_match_nothing(tmp_path):
    """
    `+ /boxmeta.toml` with `- **` matches NOTHING, because a boxmeta lives at
    `<box>/boxmeta.toml` and the leading slash anchors at the listing root.

    On a loop that runs every 15 minutes on every machine, that would make the
    LOCAL side empty, so every box in the yard would look permanently missing
    and be re-fetched on every pass, forever. The pattern has to be anchored one
    level down. Pinned so nobody "simplifies" it back.
    """
    import json
    import subprocess

    from boxyard._utils import get_rclone_binary

    root = tmp_path / "store"
    (root / "boxes" / "b1").mkdir(parents=True)
    (root / "boxes" / "b1" / const.BOX_METAFILE_REL_PATH).write_text('name="x"\n')
    (root / "boxes" / "b1" / "stray.txt").write_text("stray\n")
    conf = tmp_path / "rclone.conf"
    conf.write_text(f"[loc]\ntype = alias\nremote = {root}\n")

    def listing(*filters):
        out = subprocess.run(
            [
                get_rclone_binary(), "lsjson", "--config", str(conf),
                "--files-only", "--recursive", "--max-depth", "2",
                *[a for f in filters for a in ("--filter", f)],
                "loc:boxes",
            ],
            capture_output=True, text=True,
        )
        return sorted(e["Path"] for e in json.loads(out.stdout or "[]"))

    # What the code does now.
    assert listing(f"+ /*/{const.BOX_METAFILE_REL_PATH}", "- **") == [
        f"b1/{const.BOX_METAFILE_REL_PATH}"
    ]
    # The tempting simplification, which finds nothing at all.
    assert listing(f"+ /{const.BOX_METAFILE_REL_PATH}", "- **") == []
    # And the original defect: no terminating exclude lets the stray through.
    assert "b1/stray.txt" in listing(f"+ {const.BOX_METAFILE_REL_PATH}")


def test_a_stray_file_in_the_local_store_does_not_mask_a_missing_boxmeta(
    two_machines,
):
    """
    The two listings are DIFFED, so they must ask the same question of both
    sides. If only one filtered strays, a file present on one side and not the
    other would shift the difference and either invent or hide a missing box.

    Asserted behaviourally rather than by reading the source, because what
    matters is that the two sides agree, not how they are spelled.
    """
    config = get_config(two_machines["cpB"])
    local_box = (
        config.local_store_path / two_machines["remote_name"] / two_machines["idx"]
    )
    local_box.mkdir(parents=True, exist_ok=True)
    (local_box / "data.snapshot").write_text("a stray on the LOCAL side\n")

    missing = run(
        sync_missing_boxmetas(config_path=two_machines["cpB"], verbose=False)
    )

    assert missing == [f"{two_machines['idx']}/{const.BOX_METAFILE_REL_PATH}"], (
        "a local stray changed which boxes looked missing"
    )


# %% [markdown]
# ## `--skip-unchanged-meta` may only skip a META-only pass
#
# The filter proves something about META and nothing about DATA or CONF. It used
# to drop the box from the pass entirely, so a FULL pass with the flag on would
# skip a box whose DATA had changed locally. Latent only because the flag has
# never been switched on.

# %%
#|export
def _multi_sync(config_path, *args):
    from typer.testing import CliRunner

    from boxyard._cli.app import app

    result = CliRunner().invoke(
        app, ["--config", str(config_path), "multi-sync", *args]
    )
    assert result.exit_code == 0, f"exited {result.exit_code}\n{result.output}"
    return result


def test_a_full_pass_does_not_skip_data_on_meta_evidence(two_machines):
    """
    The box's boxmeta is settled but its DATA has changed locally. A full pass
    with `--skip-unchanged-meta` must still sync the DATA.
    """
    from boxyard._models import BoxMeta

    cpB = two_machines["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    _multi_sync(cpB, "-c", "meta")
    _multi_sync(cpB, "-c", "meta", "--skip-unchanged-meta")

    config = get_config(cpB)
    meta = BoxMeta.load(config, two_machines["remote_name"], two_machines["idx"])
    data = meta.get_local_part_path(config, BoxPart.DATA)
    if not data.is_dir():
        from boxyard.cmds import include_box

        run(include_box(config_path=cpB, box_index_name=two_machines["idx"],
                        read_only=True))
        data = meta.get_local_part_path(get_config(cpB), BoxPart.DATA)

    result = _multi_sync(cpB, "--skip-unchanged-meta")
    assert "no box was skipped" in result.output, (
        "a full pass silently skipped boxes on META evidence alone"
    )


def test_a_meta_only_pass_still_skips(two_machines):
    """The flag's actual purpose -- the fast META loop -- is unaffected."""
    cpB = two_machines["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    _multi_sync(cpB, "-c", "meta")
    result = _multi_sync(cpB, "-c", "meta", "--skip-unchanged-meta")
    assert "no box was skipped" not in result.output


# %% [markdown]
# ## multi-sync's own listing has the same defect
#
# Its listing keys by BOX, so a second file at depth 2 does not merely come back
# — it OVERWRITES the boxmeta's entry. The META check record is then stamped with
# the stray's `(ModTime, Size)`, and the box can never match on a later pass:
# the optimisation silently switches itself off.

# %%
#|export
def test_the_meta_stamp_describes_the_boxmeta_not_a_stray(two_machines):
    """
    Observed through the check record, which is what the next pass compares.
    The stray is given a deliberately different size so the two are
    distinguishable.
    """
    from boxyard._sync_policy import read_check_record

    stray = two_machines["box_root"] / "data.snapshot"
    stray.write_text("x" * 500 + "\n")
    boxmeta_size = (
        two_machines["box_root"] / const.BOX_METAFILE_REL_PATH
    ).stat().st_size
    assert stray.stat().st_size != boxmeta_size, "precondition: sizes differ"

    cpB = two_machines["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    _multi_sync(cpB, "-c", "meta")
    _multi_sync(cpB, "-c", "meta", "--skip-unchanged-meta")

    record = read_check_record(
        get_config(cpB), two_machines["idx"], BoxPart.META
    )
    assert record is not None
    assert record["remote_size"] == boxmeta_size, (
        f"the META stamp was taken from the stray file "
        f"({record['remote_size']} vs the boxmeta's {boxmeta_size})"
    )


def test_a_box_with_a_stray_is_still_skippable_for_meta(two_machines):
    """
    The consequence that matters: with the stamp taken from the right file, the
    box is provably unchanged on the next pass. With it taken from the stray,
    it never is.
    """
    from boxyard._sync_policy import meta_boxes_needing_sync
    from boxyard._models import get_boxyard_meta as _meta

    (two_machines["box_root"] / "data.snapshot").write_text("x" * 500 + "\n")

    cpB = two_machines["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    _multi_sync(cpB, "-c", "meta")
    _multi_sync(cpB, "-c", "meta", "--skip-unchanged-meta")

    config = get_config(cpB)
    import json
    import subprocess

    from boxyard._utils import get_rclone_binary

    out = subprocess.run(
        [
            get_rclone_binary(), "lsjson", "--config",
            str(config.rclone_config_path), "--files-only", "--recursive",
            "--max-depth", "2",
            "--filter", f"+ /*/{const.BOX_METAFILE_REL_PATH}", "--filter", "- **",
            f"{two_machines['remote_name']}:"
            f"{config.storage_locations[two_machines['remote_name']].store_path}/"
            f"{const.REMOTE_BOXES_REL_PATH}",
        ],
        capture_output=True, text=True,
    )
    listing = {
        Path(e["Path"]).parts[0]: (e.get("ModTime"), e.get("Size"))
        for e in json.loads(out.stdout or "[]")
    }
    _, skippable = meta_boxes_needing_sync(
        config, _meta(config).box_metas, listing
    )
    assert two_machines["idx"] in skippable

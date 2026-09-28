# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Stale duplicate registrations
#
# A rename keeps the box id and changes only the name half of the index name.
# `sync-missing-meta` reconciles on the id and adopts a remote rename, but a
# machine that was not around for the rename -- or ran a version that diffed on
# raw names -- can hold TWO registrations for one id: the stale pre-rename
# name beside the name the remote has. Doctor reports it as `duplicate-box-id`,
# and until now the only way out was by hand, because `boxyard delete` on the
# stale name would tombstone the id, i.e. the live box, on every machine.
#
# `sync-missing-meta` now drops the stale registration itself, but only when
# that is provably free of loss: the remote has exactly one name for the id,
# that name is registered here too, and the stale registration holds no DATA
# under a checkout root. Everything else is left for a human, loudly.

# %%
#|default_exp integration.cmds.test_stale_duplicate_registrations

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import asyncio
import shutil

import pytest

from boxyard import const
from boxyard._checkout import PlacementState, load_placement
from boxyard._models import BoxMeta, get_boxyard_meta
from boxyard.cmds import exclude_box, new_box, run_doctor, sync_box, sync_missing_boxmetas


def run(coro):
    return asyncio.run(coro)


def _registration_dir(config, sl, index_name):
    return config.local_store_path / sl / index_name


def _records_dir(config, index_name):
    return config.boxyard_data_path / const.SYNC_RECORDS_REL_PATH / index_name


def _fabricate_stale_twin(config, sl, live_index_name, old_name):
    """
    Leave what an older `sync-missing-meta` left: the live registration copied
    under the pre-rename name -- boxmeta mirror plus META sync records -- and
    nothing else, because the box was never included under that name.
    """
    stale_index_name = f"{BoxMeta.extract_box_id(live_index_name)}__{old_name}"
    shutil.copytree(
        _registration_dir(config, sl, live_index_name),
        _registration_dir(config, sl, stale_index_name),
    )
    if _records_dir(config, live_index_name).is_dir():
        shutil.copytree(
            _records_dir(config, live_index_name), _records_dir(config, stale_index_name)
        )
    return stale_index_name


def _registrations_for(config, box_id):
    return sorted(
        bm.index_name
        for bm in get_boxyard_meta(config, force_create=True).box_metas
        if bm.box_id == box_id
    )

# %% [markdown]
# ## The stale twin holds no DATA: dropped, and the live box is untouched
#
# Parametrized over the two shapes it takes: the live box INCLUDED here (the
# stale twin then reads MISSING, since the placement record is keyed by id and
# names a root where no directory exists under the old name) and EXCLUDED (the
# stale twin reads EXCLUDED too).

# %%
#|export
@pytest.mark.integration
@pytest.mark.parametrize("live_box_included", [True, False])
def test_a_stale_twin_with_no_data_here_is_dropped(temp_boxyard, live_box_included):
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    live = new_box(config_path=config_path, box_name="after-rename", storage_location=remote_name)
    run(sync_box(config_path=config_path, box_index_name=live))
    if not live_box_included:
        run(exclude_box(config_path=config_path, box_index_name=live))
    box_id = BoxMeta.extract_box_id(live)
    stale = _fabricate_stale_twin(config, remote_name, live, "before-rename")
    assert _registrations_for(config, box_id) == sorted([live, stale])

    # Doctor sees the duplicate and sends people to the command that fixes it.
    report = run(run_doctor(config_path=config_path, check_remote=False))
    dup = report["checks"]["duplicate-box-id"]["findings"]
    assert len(dup) == 1
    assert "boxyard sync-missing-meta" in dup[0]["hint"]
    assert "Never `boxyard delete`" in dup[0]["hint"]

    run(sync_missing_boxmetas(config_path=config_path))

    assert not _registration_dir(config, remote_name, stale).exists()
    assert not _records_dir(config, stale).exists()
    assert _registration_dir(config, remote_name, live).exists()
    assert _records_dir(config, live).is_dir()
    assert _registrations_for(config, box_id) == [live]
    live_meta = BoxMeta.load(config, remote_name, live)
    expected_state = PlacementState.INCLUDED if live_box_included else PlacementState.EXCLUDED
    assert load_placement(config, live_meta).state == expected_state
    if live_box_included:
        assert (config.user_boxes_path / live).is_dir()

    report = run(run_doctor(config_path=config_path, check_remote=False))
    assert not report["checks"]["duplicate-box-id"]["findings"]
    assert not report["checks"]["orphaned-sync-records"]["findings"]
    assert not report["checks"]["stale-cache"]["findings"]

# %% [markdown]
# ## The stale twin has a directory under a checkout root: left for a human
#
# Whatever is in that directory is not provably anywhere else, so nothing is
# removed, and doctor's hint says what to do with it.

# %%
#|export
@pytest.mark.integration
def test_a_stale_twin_that_has_data_here_is_left_alone(temp_boxyard):
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    live = new_box(config_path=config_path, box_name="after-rename", storage_location=remote_name)
    run(sync_box(config_path=config_path, box_index_name=live))
    stale = _fabricate_stale_twin(config, remote_name, live, "before-rename")
    stale_dir = config.user_boxes_path / stale
    stale_dir.mkdir()
    (stale_dir / "work.txt").write_text("only here, under the old name")

    run(sync_missing_boxmetas(config_path=config_path))

    assert _registration_dir(config, remote_name, stale).exists()
    assert (stale_dir / "work.txt").read_text() == "only here, under the old name"
    report = run(run_doctor(config_path=config_path, check_remote=False))
    dup = report["checks"]["duplicate-box-id"]["findings"]
    assert len(dup) == 1
    assert "move that directory out of the root" in dup[0]["hint"]

# %% [markdown]
# ## Two remote names for one id: nothing is dropped
#
# A half-renamed remote is the ambiguity this pass has always refused to guess
# at, and dropping a local registration on top of it would only add a guess.

# %%
#|export
@pytest.mark.integration
def test_two_remote_names_for_one_id_drops_nothing(temp_boxyard):
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    live = new_box(config_path=config_path, box_name="after-rename", storage_location=remote_name)
    run(sync_box(config_path=config_path, box_index_name=live))
    stale = _fabricate_stale_twin(config, remote_name, live, "before-rename")
    remote_boxes = remote_rclone_path / "boxyard" / const.REMOTE_BOXES_REL_PATH
    shutil.copytree(remote_boxes / live, remote_boxes / stale)

    run(sync_missing_boxmetas(config_path=config_path))

    assert _registration_dir(config, remote_name, stale).exists()
    assert _registration_dir(config, remote_name, live).exists()

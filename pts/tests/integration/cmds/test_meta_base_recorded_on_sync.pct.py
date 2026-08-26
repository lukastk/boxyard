# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # A Real Sync Records the Merge Base
#
# The unit tests cover `record_meta_base` in isolation. This covers the part
# that cannot be unit tested: that `sync_box` actually CALLS it, and calls it
# only where local and remote are known to match.
#
# Getting that condition wrong in the permissive direction is the dangerous
# failure. A base recorded when the two sides differ never corresponded to a
# shared state, and a merge diffing against it would be confidently wrong —
# which is worse than the refusal it replaces.

# %%
#|default_exp integration.cmds.test_meta_base_recorded_on_sync

# %%
#|export
import asyncio
import tomllib

import pytest

from boxyard.cmds import new_box, sync_box, modify_boxmeta
from boxyard._models import get_boxyard_meta
from boxyard.config import get_config

# %% [markdown]
# ## A push records it, and a second sync keeps it current

# %%
#|export
@pytest.mark.integration
def test_sync_records_the_meta_base(temp_boxyard):
    remote_name, _, config, config_path, _ = temp_boxyard
    index_name = new_box(
        config_path=config_path, box_name="synced", storage_location=remote_name
    )
    config = get_config(config_path)
    box = get_boxyard_meta(config).by_index_name[index_name]
    base_path = box.get_local_meta_base_path(config)

    # A brand-new box has never synced, so there is nothing agreed yet.
    assert not base_path.exists()

    asyncio.run(sync_box(config_path=config_path, box_index_name=index_name))
    assert base_path.is_file(), "the push did not record a base"
    # Compared against the box's OWN groups rather than a literal: this shell
    # exports DEFAULT_BOX_GROUPS, so a new box here is not groupless.
    _initial = list(get_boxyard_meta(get_config(config_path)).by_index_name[index_name].groups)
    assert tomllib.loads(base_path.read_text())["groups"] == _initial

    # An edit alone does not move the base -- that is the whole point, it is
    # what the last SYNC agreed, not what the file currently says.
    modify_boxmeta(
        config_path=config_path,
        box_index_name=index_name,
        modifications={"groups": _initial + ["archived"]},
    )
    assert tomllib.loads(base_path.read_text())["groups"] == _initial

    # Pushing that edit moves it.
    asyncio.run(sync_box(config_path=config_path, box_index_name=index_name))
    assert tomllib.loads(base_path.read_text())["groups"] == _initial + ["archived"]


@pytest.mark.integration
def test_a_sync_with_nothing_to_do_still_records_the_base(temp_boxyard):
    """The SYNCED case.

    Most boxes on most passes are already in sync, so if the base were only
    written on an actual transfer, almost nothing would ever get one.
    """
    remote_name, _, config, config_path, _ = temp_boxyard
    index_name = new_box(
        config_path=config_path, box_name="quiet", storage_location=remote_name
    )
    asyncio.run(sync_box(config_path=config_path, box_index_name=index_name))

    config = get_config(config_path)
    base_path = get_boxyard_meta(config).by_index_name[index_name].get_local_meta_base_path(config)
    base_path.unlink()

    # Nothing to transfer this time; the base must still be recorded.
    asyncio.run(sync_box(config_path=config_path, box_index_name=index_name))
    assert base_path.is_file(), "an already-synced box never got a base"

# %% [markdown]
# ## A refused sync must NOT move the base
#
# This is the dangerous direction, and the one a mutation test caught nothing
# for until this existed: recording a base while the two sides DIFFER. Such a
# base never corresponded to a shared state, so a merge diffing against it
# would compute the wrong deltas — confidently, and with no way to notice.
# Declining to record leaves the merge with nothing, which only costs a
# refusal.
#
# The state forged here is the real one: a remote META record marked
# `sync_complete = False` from another machine, which is exactly how 44 boxes
# sat wedged on macbook. Forging the record is how the other divergence tests
# do it too — racing two yards into a real conflict takes a day of wall clock.
#
# What currently enforces this is that the refusal RAISES, before the recording
# line is reached; the condition guarding that line is defence in depth against
# a future refusal that returns a status instead (see `SyncCondition.
# WRITE_DENIED` for the precedent). This test pins the PROPERTY rather than
# either mechanism, so it keeps its meaning if the mechanism changes.

# %%
#|export
@pytest.mark.integration
def test_a_refused_sync_leaves_the_base_alone(temp_boxyard):
    from boxyard import const
    from boxyard._models import BoxPart, SyncRecord
    from ulid import ULID

    remote_name, remote_rclone_path, _, config_path, _ = temp_boxyard
    index_name = new_box(
        config_path=config_path, box_name="wedged", storage_location=remote_name
    )
    asyncio.run(sync_box(config_path=config_path, box_index_name=index_name))

    config = get_config(config_path)
    box = get_boxyard_meta(config).by_index_name[index_name]
    base_path = box.get_local_meta_base_path(config)
    agreed = base_path.read_text()

    # Edit locally, then wedge the remote: an incomplete push from a machine
    # that is not this one, which sync must refuse rather than resolve.
    modify_boxmeta(
        config_path=config_path,
        box_index_name=index_name,
        modifications={"groups": ["archived-after-the-base"]},
    )
    remote_rec = (
        remote_rclone_path
        / config.storage_locations[remote_name].store_path
        / const.SYNC_RECORDS_REL_PATH
        / index_name
        / f"{BoxPart.META.value}.rec"
    )
    assert remote_rec.exists(), f"expected a pushed record at {remote_rec}"
    remote_rec.write_text(
        SyncRecord(
            ulid=ULID(), sync_complete=False, syncer_hostname="some-other-machine"
        ).model_dump_json()
    )

    with pytest.raises(Exception):
        asyncio.run(sync_box(config_path=config_path, box_index_name=index_name))

    assert base_path.read_text() == agreed, (
        "a refused sync moved the merge base, so a later merge would diff "
        "against a state that never existed"
    )

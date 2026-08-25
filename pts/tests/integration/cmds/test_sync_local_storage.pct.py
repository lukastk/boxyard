# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Syncing a Box in a `local` Storage Location
#
# A `local` storage location has no remote — its store is a directory on this
# machine — so there is nothing to sync. `sync_box` bailed out of that case with
# a bare `return`, while its signature promises
# `dict[BoxPart, tuple[SyncStatus, bool]]` and every caller reads one.
#
# `multi-sync` calls `.values()` on the result, so one local-storage box became
# an `AttributeError` caught into a red `Error` line — repeated every 1200s
# under supervisor, on every machine, for a state that is working as designed.
#
# Latent rather than live: v0.5.4 was what made `local` storage locations
# usable at all (`init` had never created their `local_store` entry), so the
# path only became reachable then.

# %%
#|default_exp integration.cmds.test_sync_local_storage

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import asyncio
import tomllib
from pathlib import Path

import pytest
import tomli_w

from boxyard._models import BoxPart, SyncCondition
from boxyard.cmds import get_box_sync_status, init_boxyard, new_box, sync_box


def _local_only_boxyard(tmp_path: Path) -> Path:
    """A boxyard whose only storage location is the default `local` one."""
    config_path = tmp_path / ".config" / "boxyard" / "config.toml"
    init_boxyard(config_path=config_path, data_path=tmp_path / ".boxyard", verbose=False)
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    data["user_boxes_path"] = (tmp_path / "user_boxes").as_posix()
    data["user_box_groups_path"] = (tmp_path / "user_box_groups").as_posix()
    config_path.write_text(tomli_w.dumps(data))
    return config_path

# %% [markdown]
# ## The result is a dict, one entry per requested part

# %%
#|export
@pytest.mark.integration
def test_sync_box_local_storage_returns_a_result(tmp_path):
    config_path = _local_only_boxyard(tmp_path)
    index_name = new_box(
        config_path=config_path, box_name="local-only", initialise_git=False
    )

    # TESTREF: test_sync_box_local_storage_returns_a_result
    results = asyncio.run(sync_box(config_path=config_path, box_index_name=index_name))

    assert results is not None, "sync_box must return a dict, not None"
    assert set(results) == set(BoxPart)
    for part, (status, synced) in results.items():
        assert status.sync_condition == SyncCondition.LOCAL_STORAGE, part
        assert synced is False, part

    # This is exactly what `multi-sync` does with the result, and exactly what
    # used to raise AttributeError.
    assert not any(
        status.sync_condition == SyncCondition.WRITE_DENIED
        for status, _ in results.values()
    )

# %% [markdown]
# ## `sync_choices` is still honoured

# %%
#|export
@pytest.mark.integration
def test_sync_box_local_storage_honours_sync_choices(tmp_path):
    config_path = _local_only_boxyard(tmp_path)
    index_name = new_box(
        config_path=config_path, box_name="local-parts", initialise_git=False
    )

    results = asyncio.run(
        sync_box(
            config_path=config_path,
            box_index_name=index_name,
            sync_choices=[BoxPart.DATA],
        )
    )
    assert set(results) == {BoxPart.DATA}

# %% [markdown]
# ## `box-status` reports the same thing, instead of failing in rclone
#
# `get_sync_status` is handed `remote=<storage_location>` as an rclone remote
# NAME, and a `local` storage location has no section in
# `boxyard_rclone.conf` — so `box-status`, `yard-status` and `list --status`
# all died with `didn't find section in config file`.

# %%
#|export
@pytest.mark.integration
def test_box_status_local_storage(tmp_path):
    config_path = _local_only_boxyard(tmp_path)
    index_name = new_box(
        config_path=config_path, box_name="local-status", initialise_git=False
    )

    # TESTREF: test_box_status_local_storage
    statuses = asyncio.run(
        get_box_sync_status(config_path=config_path, box_index_name=index_name)
    )

    assert set(statuses) == set(BoxPart)
    for part, status in statuses.items():
        assert status.sync_condition == SyncCondition.LOCAL_STORAGE, part
        assert status.error_message is None, part

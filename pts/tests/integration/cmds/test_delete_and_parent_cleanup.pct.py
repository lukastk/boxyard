# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Integration tests: delete cleanup + remove-parent for dangling parents
#
# Covers boxyard#13 (delete must not leave orphaned sync records) and boxyard#14
# (`remove-parent --parent-id` must drop a dangling parent whose box is gone).

# %%
#|default_exp integration.cmds.test_delete_and_parent_cleanup

# %%
#|export
import asyncio

import pytest

from boxyard.cmds import new_box, sync_box, delete_box, modify_boxmeta
from boxyard._models import get_boxyard_meta, BoxPart


# %%
#|export
@pytest.mark.integration
def test_delete_removes_sync_records(temp_boxyard):
    """boxyard#13: deleting a box also removes its sync-record directory."""
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    idx = new_box(config_path=config_path, box_name="rec-box", storage_location=remote_name)
    asyncio.run(sync_box(config_path=config_path, box_index_name=idx))

    bm = get_boxyard_meta(config).by_index_name[idx]
    local_sync_dir = bm.get_local_sync_record_path(config, BoxPart.DATA).parent
    assert local_sync_dir.exists(), "sync should have created local sync records"

    asyncio.run(delete_box(config_path=config_path, box_index_name=idx))

    assert not local_sync_dir.exists(), "delete must not leave orphaned sync records"


# %% [markdown]
# ## A remote purge that fails must abort the delete
#
# `delete_box` already carried the comment "Keep registration + placement until
# all remote work succeeds. If remote deletion fails after DATA removal, doctor
# sees an included-but-missing checkout and the command can be retried without
# losing the box identity." — and then discarded the purge's return value, so
# there was nothing for it to abort on. An unreachable remote silently left the
# box's data sitting there forever while the delete reported success and removed
# the registration, which was the only record of where that data was.

# %%
#|export
@pytest.mark.integration
def test_delete_aborts_when_the_remote_purge_fails(temp_boxyard):
    from unittest.mock import AsyncMock, patch

    from boxyard._utils.rclone import RcloneFailed

    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    idx = new_box(config_path=config_path, box_name="doomed-box", storage_location=remote_name)
    asyncio.run(sync_box(config_path=config_path, box_index_name=idx))
    registration = config.local_store_path / remote_name / idx
    assert registration.exists()

    with patch(
        "boxyard._utils.rclone_purge_absent_ok",
        new=AsyncMock(side_effect=RcloneFailed(["rclone", "purge"], 1, "", "connection refused")),
    ):
        with pytest.raises(RcloneFailed):
            asyncio.run(delete_box(config_path=config_path, box_index_name=idx))

    # The identity survives, so the delete can simply be retried.
    assert registration.exists(), (
        "a failed remote purge must not take the registration with it -- without "
        "it there is no way left to find the data the purge did not delete"
    )


@pytest.mark.integration
def test_delete_succeeds_when_the_remote_paths_were_never_there(temp_boxyard):
    """
    The other half of the same change: absence is NOT failure. A box that was
    never pushed has no remote directory, and `sync_backups/<index_name>` never
    exists at all under the current layout (backups are keyed by sync ULID), so
    a delete that raised on a missing path would raise on every ordinary delete.
    """
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    idx = new_box(config_path=config_path, box_name="never-pushed", storage_location=remote_name)
    asyncio.run(delete_box(config_path=config_path, box_index_name=idx))

    assert not (config.local_store_path / remote_name / idx).exists()
    assert idx not in get_boxyard_meta(config).by_index_name


# %%
#|export
@pytest.mark.integration
def test_remove_parent_drops_dangling_parent(temp_boxyard):
    """boxyard#14: `remove-parent --parent-id` drops a parent whose box no longer exists."""
    from typer.testing import CliRunner
    from boxyard._cli.main import app

    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard
    runner = CliRunner()

    child = new_box(config_path=config_path, box_name="child", storage_location=remote_name)

    # Point the child at a parent id that does not correspond to any existing box —
    # exactly the state a deleted parent leaves behind (modify_boxmeta warns but allows it).
    fake_parent_id = "20200101_000000_zzzzz"
    modify_boxmeta(
        config_path=config_path,
        box_index_name=child,
        modifications={"parents": [fake_parent_id]},
    )
    assert fake_parent_id in get_boxyard_meta(config).by_index_name[child].parents

    # Before the fix this errored with "Box with id ... not found". Now it succeeds.
    result = runner.invoke(
        app,
        ["--config", str(config_path), "remove-parent", "-r", child,
         "--parent-id", fake_parent_id, "--no-refresh-user-symlinks"],
    )
    assert result.exit_code == 0, result.output
    assert get_boxyard_meta(config).by_index_name[child].parents == []


# %%
#|export
@pytest.mark.integration
def test_remove_parent_reports_when_not_a_parent(temp_boxyard):
    """Removing a parent id the box doesn't have still fails cleanly (exit 1)."""
    from typer.testing import CliRunner
    from boxyard._cli.main import app

    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard
    runner = CliRunner()

    child = new_box(config_path=config_path, box_name="child2", storage_location=remote_name)
    result = runner.invoke(
        app,
        ["--config", str(config_path), "remove-parent", "-r", child,
         "--parent-id", "20200101_000000_zzzzz", "--no-refresh-user-symlinks"],
    )
    assert result.exit_code == 1
    assert "does not have parent" in result.output

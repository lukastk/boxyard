# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # A change that happens ALONE must propagate
#
# The 0.8.0 headline bug: the mtime walk saw 2 of 10 change shapes, so a lone
# deletion was never pushed — it reached the remote only by riding along with a
# later mtime-visible change. These tests pin the WIRING in `get_sync_status`,
# not the fingerprint internals (`test_fingerprint` owns those): mutation
# testing showed both decision sites could be reverted to the mtime test with
# no test noticing, because every existing deletion test either had another
# change alongside or ran on the restic backend.
#
# Two tests, one per decision site: records-match → NEEDS_PUSH, and
# records-mismatch-remote-newer → CONFLICT (not NEEDS_PULL, which would
# silently restore the deleted file).

# %%
#|default_exp integration.sync.test_lone_changes_propagate

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import asyncio
import tempfile
from pathlib import Path

import pytest

from boxyard._enums import SyncDirection, SyncSetting
from boxyard._models import SyncCondition, get_sync_status
from boxyard._utils.sync_helper import sync_helper


# %%
#|export
def _fixture(tmp_path: Path) -> "tuple[dict, Path]":
    """A local tree, an alias remote, and the paths sync_helper needs."""
    local = tmp_path / "local"
    local.mkdir()
    (local / "keep.txt").write_text("keep")
    (local / "doomed.txt").write_text("doomed")
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    rclone_conf = tmp_path / "rclone.conf"
    rclone_conf.write_text(f"[my_remote]\ntype = alias\nremote = {remote_root}\n")
    backups = tmp_path / "backups"
    backups.mkdir()
    return dict(
        rclone_config_path=rclone_conf,
        local_path=local,
        local_sync_record_path=tmp_path / "local_data.rec",
        remote="my_remote",
        remote_path="data",
        remote_sync_record_path="data.rec",
        local_sync_backups_path=backups,
        remote_sync_backups_path="sync_backups",
    ), remote_root


async def _status(args) -> SyncCondition:
    status = await get_sync_status(
        rclone_config_path=args["rclone_config_path"],
        local_path=args["local_path"],
        local_sync_record_path=args["local_sync_record_path"],
        remote=args["remote"],
        remote_path=args["remote_path"],
        remote_sync_record_path=args["remote_sync_record_path"],
    )
    return status.sync_condition


# %%
#|export
@pytest.mark.integration
def test_a_lone_deletion_reads_needs_push_and_actually_propagates():
    """
    The records-match decision site. A deletion leaves the newest mtime exactly
    where it was, so the mtime test blesses it as SYNCED and the remote keeps
    the file for ever — measured against the live remote before 0.8.0. With a
    baseline, the digest differs and the box must read NEEDS_PUSH; the push
    must then remove the file from the remote.
    """

    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))
            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                **args,
            )
            assert (remote_root / "data" / "doomed.txt").exists()

            # The lone change: nothing else moves.
            (args["local_path"] / "doomed.txt").unlink()

            assert await _status(args) == SyncCondition.NEEDS_PUSH
            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                **args,
            )
            assert not (remote_root / "data" / "doomed.txt").exists()
            assert await _status(args) == SyncCondition.SYNCED

    asyncio.run(_test())


# %%
#|export
@pytest.mark.integration
def test_a_lone_deletion_plus_a_remote_advance_reads_conflict_not_needs_pull():
    """
    The second decision site, whose mtime version was the same bug wearing the
    other branch: delete a file locally, let another machine push, and "not
    locally modified" reads NEEDS_PULL — and the pull SILENTLY RESTORES THE
    DELETED FILE. With a baseline the deletion counts as local modification, so
    the box must read CONFLICT and refuse to pick a side.
    """

    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))

            # Machine A pushes; machine B pulls a full copy with a baseline.
            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                **args,
            )
            b_args = dict(
                args,
                local_path=Path(td) / "b_local",
                local_sync_record_path=Path(td) / "b_data.rec",
            )
            await sync_helper(
                sync_direction=SyncDirection.PULL,
                sync_setting=SyncSetting.CAREFUL,
                local_absence_means_excluded=False,
                **{k: v for k, v in b_args.items()},
            )

            # B's lone deletion, then A moves the remote on.
            (b_args["local_path"] / "doomed.txt").unlink()
            (args["local_path"] / "from-a.txt").write_text("a moved on")
            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                **args,
            )

            assert await _status(b_args) == SyncCondition.CONFLICT

    asyncio.run(_test())

# %% [markdown]
# ## A box whose content the OLD test cannot measure is not an error
#
# `get_sync_status` used to raise ERROR for "exists, not empty, but no
# measurable mtime" — a state that is always legitimate: a directory holding
# only excluded entries (a lone `.venv/`), only symlinks (the mtime walk skips
# them), or only empty directories. Every such box produced a red Error line
# on every supervisor pass for ever. The fingerprint sees all of these shapes,
# so the guard now has no true positive left.

# %%
#|export
@pytest.mark.integration
def test_a_box_holding_only_unmeasurable_content_is_not_an_error():
    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))
            # Replace the fixture's files with content the mtime walk cannot
            # measure: an excluded directory and a symlink.
            (args["local_path"] / "keep.txt").unlink()
            (args["local_path"] / "doomed.txt").unlink()
            (args["local_path"] / ".venv").mkdir()
            (args["local_path"] / ".venv" / "pyvenv.cfg").write_text("home = /usr")
            (args["local_path"] / "link").symlink_to("somewhere/else")
            exclude_file = Path(td) / "excludes"
            exclude_file.write_text(".venv/\n")

            async def _status_x() -> SyncCondition:
                s = await get_sync_status(
                    rclone_config_path=args["rclone_config_path"],
                    local_path=args["local_path"],
                    local_sync_record_path=args["local_sync_record_path"],
                    remote=args["remote"],
                    remote_path=args["remote_path"],
                    remote_sync_record_path=args["remote_sync_record_path"],
                    exclude_path=exclude_file,
                )
                return s.sync_condition

            # Never synced: pending work, not something wrong.
            assert await _status_x() == SyncCondition.NEEDS_PUSH

            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                exclude_path=exclude_file,
                **args,
            )
            # The symlink travelled (a local-backend remote round-trips it
            # back to a real symlink rather than a `.rclonelink` file); the
            # excluded content did not.
            assert (remote_root / "data" / "link").is_symlink()
            assert not (remote_root / "data" / ".venv").exists()
            assert await _status_x() == SyncCondition.SYNCED

    asyncio.run(_test())

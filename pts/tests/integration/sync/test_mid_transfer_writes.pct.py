# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Writes that race a running transfer
#
# The 0.8.1 bug class, on the plain backend: state recorded at transfer
# COMPLETION describes a window in which the transfer was still reading the
# tree. A file written in that window is on disk but not (reliably) on the
# remote; blessing it into the fingerprint baseline makes the next status
# check read SYNCED and the change is invisible until the file is touched
# again -- or, on the pull side, until the next pull silently deletes it.
#
# So: the PUSH baseline is fingerprinted BEFORE the transfer (the tree state
# the push acted on), and the PULL baseline is refused entirely when the tree
# changed while the pull ran (leaving "no usable baseline", i.e. the old mtime
# test, which the racing write's fresh mtime keeps loud).
#
# Both tests simulate the race by wrapping `rclone_sync`: the real transfer
# runs, and the racing file is planted before the wrapper returns -- i.e. after
# rclone has walked the tree, before `sync_helper` records anything.

# %%
#|default_exp integration.sync.test_mid_transfer_writes

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import boxyard._utils
from boxyard._enums import SyncDirection, SyncSetting
from boxyard._fingerprint import base_path_for
from boxyard._models import SyncCondition, get_sync_status
from boxyard._utils.sync_helper import sync_helper


# %%
#|export
def _fixture(tmp_path: Path) -> "tuple[dict, Path]":
    """A local tree, an alias remote, and the paths sync_helper needs."""
    local = tmp_path / "local"
    local.mkdir()
    (local / "file1.txt").write_text("one")
    (local / "sub").mkdir()
    (local / "sub" / "file2.txt").write_text("two")
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


def _racing_rclone_sync(plant: "callable"):
    """The real transfer, then the racing write, then return."""
    real = boxyard._utils.rclone_sync

    async def wrapper(**kwargs):
        res = await real(**kwargs)
        plant()
        return res

    return wrapper


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
def test_a_file_written_during_a_push_is_not_blessed_as_pushed():
    """
    The baseline must describe the tree the push ACTED ON, not the tree at
    completion. A post-transfer fingerprint includes the racing file -- which
    the transfer never carried -- and the next check reads SYNCED with the
    remote missing the file, silently, for ever.
    """

    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))

            # An ordinary first push establishes records and a baseline.
            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                **args,
            )

            # A real change, then a push during which another write lands.
            (args["local_path"] / "file1.txt").write_text("one, edited")
            racing = args["local_path"] / "written-mid-push.txt"
            with patch(
                "boxyard._utils.rclone_sync",
                new=_racing_rclone_sync(lambda: racing.write_text("racing")),
            ):
                await sync_helper(
                    sync_direction=SyncDirection.PUSH,
                    sync_setting=SyncSetting.CAREFUL,
                    **args,
                )

            # The simulation is honest: the racing file never reached the
            # remote, and both records say the sync completed.
            assert not (remote_root / "data" / "written-mid-push.txt").exists()
            assert (remote_root / "data" / "file1.txt").read_text() == "one, edited"

            # The one assertion that matters: the box must NOT read SYNCED.
            assert await _status(args) == SyncCondition.NEEDS_PUSH

            # And the reconcile push it asks for actually heals the divergence.
            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                **args,
            )
            assert (remote_root / "data" / "written-mid-push.txt").exists()
            assert await _status(args) == SyncCondition.SYNCED

    asyncio.run(_test())


# %%
#|export
@pytest.mark.integration
def test_a_file_written_during_a_pull_is_not_blessed_and_not_later_deleted():
    """
    The pull-side sibling, and the one that regressed hardest under a
    completion-time baseline: the blessed file reads SYNCED, the next remote
    advance reads NEEDS_PULL, and that pull DELETES the file (rclone sync
    removes extraneous local files) with the backup purged on success. The old
    mtime test protected it -- the racing write postdates the adopted record --
    so the pull refuses to record a baseline at all and leaves that old test
    in charge.
    """

    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))

            # Machine A pushes; machine B (fresh paths, same remote) pulls
            # while a local write races the transfer.
            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                **args,
            )

            b_local = Path(td) / "b_local"
            b_args = dict(
                args,
                local_path=b_local,
                local_sync_record_path=Path(td) / "b_data.rec",
            )
            racing = b_local / "written-mid-pull.txt"
            with patch(
                "boxyard._utils.rclone_sync",
                new=_racing_rclone_sync(lambda: racing.write_text("racing")),
            ):
                await sync_helper(
                    sync_direction=SyncDirection.PULL,
                    sync_setting=SyncSetting.CAREFUL,
                    local_absence_means_excluded=False,
                    **{k: v for k, v in b_args.items()},
                )

            # No baseline may bless a tree the pull did not produce.
            assert not base_path_for(b_args["local_sync_record_path"]).exists()

            # The racing write must read as pending local work, not as synced.
            assert await _status(b_args) == SyncCondition.NEEDS_PUSH

            # And pushing it converges: the file reaches the remote instead of
            # being deleted by a later pull.
            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                **b_args,
            )
            assert (remote_root / "data" / "written-mid-pull.txt").exists()
            assert await _status(b_args) == SyncCondition.SYNCED

    asyncio.run(_test())


# %%
#|export
@pytest.mark.integration
def test_a_quiet_pull_still_records_a_usable_baseline():
    """
    The refusal must not overreach: a pull with no racing write records a
    baseline bound to the adopted record, under the same (empty) filter
    signature the status check reads with -- so the box answers SYNCED from
    the fingerprint alone afterwards.
    """

    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))
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

            base_path = base_path_for(b_args["local_sync_record_path"])
            assert base_path.exists()
            base = json.loads(base_path.read_text())
            rec = json.loads(Path(b_args["local_sync_record_path"]).read_text())
            assert base["sync_record_ulid"] == rec["ulid"]
            # Written under the same signature the reader uses (no exclude
            # file on either side), or the baseline would be write-only.
            from boxyard._fingerprint import filter_signature

            assert base["filter_signature"] == filter_signature(None)
            assert await _status(b_args) == SyncCondition.SYNCED

    asyncio.run(_test())

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

# %% [markdown]
# ## The two other lying-baseline windows on a pull
#
# Found by the full-pass-skip design review (2026-09-25). A skip filter TRUSTS
# the baseline, so a baseline that describes a tree the remote does not hold is
# no longer a one-pass inefficiency: it is a wrong skip, for ever.
#
# 1. The racing-write guard used the newest surviving FILE mtime, which a
#    mid-pull deletion (or rename, chmod, symlink edit) does not move.
# 2. The pull adopted the remote record read AFTER the transfer; an owner push
#    between transfer and read was adopted with the PREVIOUS revision's tree.

# %%
#|export
@pytest.mark.integration
def test_a_file_deleted_during_a_pull_is_not_blessed():
    """
    A deletion racing the pull leaves no newer file mtime behind. The guard
    is `tree_touched_since` (directory mtimes and ctimes), so the pull must
    refuse to record a baseline -- a baseline here would bless a tree that is
    MISSING a file the remote has, and read SYNCED about it.
    """

    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))
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
            with patch(
                "boxyard._utils.rclone_sync",
                new=_racing_rclone_sync(lambda: (b_local / "sub" / "file2.txt").unlink()),
            ):
                await sync_helper(
                    sync_direction=SyncDirection.PULL,
                    sync_setting=SyncSetting.CAREFUL,
                    local_absence_means_excluded=False,
                    **{k: v for k, v in b_args.items()},
                )

            assert not (b_local / "sub" / "file2.txt").exists(), "simulation"
            assert (remote_root / "data" / "sub" / "file2.txt").exists(), "simulation"
            assert not base_path_for(b_args["local_sync_record_path"]).exists(), (
                "a baseline was recorded for a tree the pull did not produce"
            )

    asyncio.run(_test())


@pytest.mark.integration
def test_a_foreign_push_during_a_pull_is_not_adopted():
    """
    The pull adopts the remote record the PRE-transfer status read. If the
    remote moves on while the transfer runs, the local record must still name
    the revision whose tree was downloaded; the next status then reads
    NEEDS_PULL and the second pull brings the new revision -- never SYNCED with
    the old files.
    """

    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))
            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                **args,
            )
            u1 = json.loads(Path(args["local_sync_record_path"]).read_text())["ulid"]

            b_args = dict(
                args,
                local_path=Path(td) / "b_local",
                local_sync_record_path=Path(td) / "b_data.rec",
            )

            async def _owner_pushes_again():
                (args["local_path"] / "file1.txt").write_text("one, pushed mid-pull")
                await sync_helper(
                    sync_direction=SyncDirection.PUSH,
                    sync_setting=SyncSetting.CAREFUL,
                    **args,
                )

            real = boxyard._utils.rclone_sync
            fired = []

            async def _racing(**kwargs):
                res = await real(**kwargs)
                if not fired:  # the owner's push goes through this patch too
                    fired.append(True)
                    await _owner_pushes_again()
                return res

            with patch("boxyard._utils.rclone_sync", new=_racing):
                await sync_helper(
                    sync_direction=SyncDirection.PULL,
                    sync_setting=SyncSetting.CAREFUL,
                    local_absence_means_excluded=False,
                    **{k: v for k, v in b_args.items()},
                )

            u2 = json.loads(Path(args["local_sync_record_path"]).read_text())["ulid"]
            assert u2 != u1, "simulation: the owner's second push minted a new record"
            assert (b_args["local_path"] / "file1.txt").read_text() == "one", "simulation"

            adopted = json.loads(Path(b_args["local_sync_record_path"]).read_text())["ulid"]
            assert adopted == u1, (
                "the pull adopted the record of a revision it did not download"
            )
            assert await _status(b_args) == SyncCondition.NEEDS_PULL

            await sync_helper(
                sync_direction=SyncDirection.PULL,
                sync_setting=SyncSetting.CAREFUL,
                **b_args,
            )
            assert (b_args["local_path"] / "file1.txt").read_text() == "one, pushed mid-pull"
            assert await _status(b_args) == SyncCondition.SYNCED

    asyncio.run(_test())


# %% [markdown]
# ## A write landing AFTER the pull's equality check
#
# The check proves the tree equal to the remote; the fingerprint that gets
# blessed must be the tree the check judged. Found by the implementation
# review: with the fingerprint taken after the check, a file written in
# between was blessed, read SYNCED, and was deleted by the next ordinary pull.

# %%
#|export
@pytest.mark.integration
def test_a_file_written_after_the_pull_check_is_not_blessed_and_not_later_deleted():
    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))
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
            real_check = boxyard._utils.rclone_would_transfer
            racing = b_local / "written-after-check.txt"

            async def _check_then_write(**kwargs):
                res = await real_check(**kwargs)
                racing.write_text("racing")
                return res

            with patch("boxyard._utils.rclone_would_transfer", new=_check_then_write):
                await sync_helper(
                    sync_direction=SyncDirection.PULL,
                    sync_setting=SyncSetting.CAREFUL,
                    local_absence_means_excluded=False,
                    **{k: v for k, v in b_args.items()},
                )

            assert racing.exists() and not (remote_root / "data" / racing.name).exists()
            # The baseline (if any) describes the tree BEFORE the write, so the
            # write reads as pending local work.
            assert await _status(b_args) == SyncCondition.NEEDS_PUSH

            # The owner moves on; B's next careful pull must not delete the file.
            (args["local_path"] / "file1.txt").write_text("one, again")
            await sync_helper(
                sync_direction=SyncDirection.PUSH,
                sync_setting=SyncSetting.CAREFUL,
                **args,
            )
            assert await _status(b_args) == SyncCondition.CONFLICT
            assert racing.exists()

    asyncio.run(_test())


# %% [markdown]
# ## The single-file guard (META's boxmeta.toml)
#
# A single-file part has no directory to dry-run; its guard is the file's own
# mtime against the pull's start. An edit landing while the pull ran must not
# be blessed. Mutation-checked by the implementation review: removing this
# guard left every existing test green.

# %%
#|export
@pytest.mark.integration
def test_a_single_file_edited_during_a_pull_is_not_blessed():
    async def _test():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a").mkdir()
            a_file = root / "a" / "boxmeta.toml"
            a_file.write_text('name = "one"\n')
            remote_root = root / "remote"
            remote_root.mkdir()
            conf = root / "rclone.conf"
            conf.write_text(f"[my_remote]\ntype = alias\nremote = {remote_root}\n")
            (root / "backups").mkdir()
            args = dict(
                rclone_config_path=conf,
                local_path=a_file,
                local_sync_record_path=root / "a_meta.rec",
                remote="my_remote",
                remote_path="box/boxmeta.toml",
                remote_sync_record_path="meta.rec",
                local_sync_backups_path=root / "backups",
                remote_sync_backups_path="sync_backups",
            )
            await sync_helper(sync_direction=SyncDirection.PUSH, sync_setting=SyncSetting.CAREFUL, **args)

            (root / "b").mkdir()
            b_file = root / "b" / "boxmeta.toml"
            b_args = dict(args, local_path=b_file, local_sync_record_path=root / "b_meta.rec")
            with patch(
                "boxyard._utils.rclone_sync",
                new=_racing_rclone_sync(lambda: b_file.write_text('name = "edited mid-pull"\n')),
            ):
                await sync_helper(
                    sync_direction=SyncDirection.PULL,
                    sync_setting=SyncSetting.CAREFUL,
                    local_absence_means_excluded=False,
                    **b_args,
                )

            assert b_file.read_text() == 'name = "edited mid-pull"\n', "simulation"
            assert not base_path_for(b_args["local_sync_record_path"]).exists(), (
                "the mid-pull edit was blessed as what the remote holds"
            )
            assert await _status(b_args) == SyncCondition.NEEDS_PUSH

    asyncio.run(_test())


# %% [markdown]
# ## A push whose first remote record write fails
#
# Both prefixes, reproduced by the implementation review: the write did not
# land (the common shape) and the write landed but rclone reported failure.
# Neither may lose the unpushed work, and neither may wedge the box.

# %%
#|export
def _failing_remote_record_write(land: bool):
    """Fail the FIRST remote record write; land the file first if asked."""
    import boxyard._utils as utils_module

    real = utils_module.rclone_copyto
    fired = []

    async def wrapper(**kwargs):
        if kwargs["dest"] and kwargs["dest_path"].endswith(".rec") and not fired:
            fired.append(True)
            if land:
                await real(**kwargs)
            return False, "", "couldn't initialise SFTP: connection limit"
        return await real(**kwargs)

    return wrapper


@pytest.mark.integration
@pytest.mark.parametrize("landed", [False, True])
def test_a_failed_first_remote_record_write_keeps_the_unpushed_work(landed):
    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))
            await sync_helper(sync_direction=SyncDirection.PUSH, sync_setting=SyncSetting.CAREFUL, **args)

            (args["local_path"] / "file1.txt").write_text("one, edited")
            (args["local_path"] / "brand_new.txt").write_text("precious")
            with patch("boxyard._utils.rclone_copyto", new=_failing_remote_record_write(landed)):
                with pytest.raises(RuntimeError, match="connection limit"):
                    await sync_helper(
                        sync_direction=None, sync_setting=SyncSetting.CAREFUL, **args
                    )

            # The work is still there, and the next ordinary careful sync
            # PUSHES it -- never pulls the remote over it, never wedges.
            assert (args["local_path"] / "brand_new.txt").read_text() == "precious"
            status, synced = await sync_helper(
                sync_direction=None, sync_setting=SyncSetting.CAREFUL, **args
            )
            assert synced
            assert (remote_root / "data" / "brand_new.txt").read_text() == "precious"
            assert (remote_root / "data" / "file1.txt").read_text() == "one, edited"
            assert (args["local_path"] / "brand_new.txt").read_text() == "precious"
            assert await _status(args) == SyncCondition.SYNCED

    asyncio.run(_test())


@pytest.mark.integration
def test_a_landed_but_failed_record_write_is_recognised_as_this_machines_push():
    """After the landed prefix the remote holds an INCOMPLETE record the local
    side never wrote; the in-flight sidecar is what tells the retry it is ours."""

    async def _test():
        with tempfile.TemporaryDirectory() as td:
            args, remote_root = _fixture(Path(td))
            await sync_helper(sync_direction=SyncDirection.PUSH, sync_setting=SyncSetting.CAREFUL, **args)
            (args["local_path"] / "file1.txt").write_text("one, edited")
            with patch("boxyard._utils.rclone_copyto", new=_failing_remote_record_write(True)):
                with pytest.raises(RuntimeError):
                    await sync_helper(sync_direction=None, sync_setting=SyncSetting.CAREFUL, **args)
            assert await _status(args) == SyncCondition.SYNC_TO_REMOTE_INCOMPLETE
            # ...and without the sidecar this would be "another machine's".
            from boxyard._remote_identity import clear_inflight_push, read_inflight_push

            assert read_inflight_push(args["local_sync_record_path"]) is not None
            status, synced = await sync_helper(sync_direction=None, sync_setting=SyncSetting.CAREFUL, **args)
            assert synced and (remote_root / "data" / "file1.txt").read_text() == "one, edited"
            assert read_inflight_push(args["local_sync_record_path"]) is None

    asyncio.run(_test())


# %% [markdown]
# ## The single-file guard sees an mtime-preserving replacement

# %%
#|export
@pytest.mark.integration
def test_a_single_file_replaced_with_its_mtime_preserved_is_not_blessed(capsys):
    """A restore or `cp -p` racing the pull: different size, same mtime. The
    mtime-against-pull-start guard accepted it; the dry run sees the size."""

    async def _test():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a").mkdir()
            a_file = root / "a" / "boxmeta.toml"
            a_file.write_text('name = "one"\n')
            remote_root = root / "remote"
            remote_root.mkdir()
            conf = root / "rclone.conf"
            conf.write_text(f"[my_remote]\ntype = alias\nremote = {remote_root}\n")
            (root / "backups").mkdir()
            args = dict(
                rclone_config_path=conf, local_path=a_file,
                local_sync_record_path=root / "a_meta.rec", remote="my_remote",
                remote_path="box/boxmeta.toml", remote_sync_record_path="meta.rec",
                local_sync_backups_path=root / "backups", remote_sync_backups_path="sync_backups",
            )
            await sync_helper(sync_direction=SyncDirection.PUSH, sync_setting=SyncSetting.CAREFUL, **args)

            (root / "b").mkdir()
            b_file = root / "b" / "boxmeta.toml"
            b_args = dict(args, local_path=b_file, local_sync_record_path=root / "b_meta.rec")

            def _replace_keeping_mtime():
                st = b_file.stat()
                b_file.write_text('name = "replaced, longer content"\n')
                import os as _os

                _os.utime(b_file, ns=(st.st_atime_ns, st.st_mtime_ns))

            with patch("boxyard._utils.rclone_sync", new=_racing_rclone_sync(_replace_keeping_mtime)):
                await sync_helper(
                    sync_direction=SyncDirection.PULL, sync_setting=SyncSetting.CAREFUL,
                    local_absence_means_excluded=False, **b_args,
                )
            assert not base_path_for(b_args["local_sync_record_path"]).exists()

            # The mtime fallback cannot see a preserved mtime, so the bare
            # verdict is SYNCED -- which is why the bless must VERIFY: the
            # next agreement attempt must refuse the baseline and say so, and
            # with no baseline the box can never be put aside by the filter.
            assert await _status(b_args) == SyncCondition.SYNCED
            await sync_helper(
                sync_direction=None, sync_setting=SyncSetting.CAREFUL,
                local_absence_means_excluded=False, bless_on_synced=True, **b_args,
            )
            assert not base_path_for(b_args["local_sync_record_path"]).exists(), (
                "a divergent single file was blessed on a fallback verdict"
            )
            out = capsys.readouterr().out
            assert "WARNING" in out and "actually differ" in out, out

    asyncio.run(_test())

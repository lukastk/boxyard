# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for rclone Command Builder

# %%
#|default_exp unit._utils.test_rclone_cmd_builder

# %%
#|export
import pytest
import asyncio
from pathlib import Path
from unittest.mock import patch, AsyncMock

from boxyard._utils.rclone import (
    BisyncResult,
    rclone_copy,
    rclone_copyto,
    rclone_sync,
    rclone_bisync,
    rclone_mkdir,
    rclone_lsjson,
    rclone_path_exists,
    rclone_purge,
    rclone_cat,
    rclone_move,
)


# ============================================================================
# Tests for BisyncResult enum
# ============================================================================

# %%
#|export
class TestBisyncResult:
    """Tests for BisyncResult enum."""

    def test_bisync_result_values(self):
        """BisyncResult has correct values."""
        assert BisyncResult.SUCCESS.value == "success"
        assert BisyncResult.CONFLICTS.value == "conflicts"
        assert BisyncResult.ERROR_NEEDS_RESYNC.value == "needs_resync"
        assert BisyncResult.ERROR_ALL_FILES_CHANGED.value == "all_files_changed"
        assert BisyncResult.ERROR_OTHER.value == "other_error"

    def test_bisync_result_count(self):
        """There are exactly 5 bisync results."""
        assert len(BisyncResult) == 5


# ============================================================================
# Tests for rclone_copy command building
# ============================================================================

# %%
#|export
class TestRcloneCopyCommand:
    """Tests for rclone_copy command building."""

    def test_basic_copy_command(self):
        """Builds basic copy command."""
        async def _test():
            result = await rclone_copy(
                rclone_config_path="/tmp/rclone.conf",
                source="myremote",
                source_path="bucket/data",
                dest="",
                dest_path="/local/path",
                return_command=True,
            )
            assert "rclone copy" in result
            assert "--config /tmp/rclone.conf" in result
            assert "myremote:bucket/data" in result
            assert "/local/path" in result
            assert "--links" in result
            assert "--fast-list" in result

        asyncio.run(_test())

    def test_copy_with_dry_run(self):
        """Copy command with dry-run flag."""
        async def _test():
            result = await rclone_copy(
                rclone_config_path="/tmp/rclone.conf",
                source="remote",
                source_path="path",
                dest="",
                dest_path="/dest",
                dry_run=True,
                return_command=True,
            )
            assert "--dry-run" in result

        asyncio.run(_test())

    def test_copy_with_progress(self):
        """Copy command with progress flag."""
        async def _test():
            result = await rclone_copy(
                rclone_config_path="/tmp/rclone.conf",
                source="remote",
                source_path="path",
                dest="",
                dest_path="/dest",
                progress=True,
                return_command=True,
            )
            assert "--progress" in result

        asyncio.run(_test())

    def test_copy_with_include_patterns(self):
        """Copy command with include patterns."""
        async def _test():
            result = await rclone_copy(
                rclone_config_path="/tmp/rclone.conf",
                source="remote",
                source_path="path",
                dest="",
                dest_path="/dest",
                include=["*.py", "*.txt"],
                return_command=True,
            )
            assert "--include '*.py'" in result
            assert "--include '*.txt'" in result

        asyncio.run(_test())

    def test_copy_with_exclude_patterns(self):
        """Copy command with exclude patterns."""
        async def _test():
            result = await rclone_copy(
                rclone_config_path="/tmp/rclone.conf",
                source="remote",
                source_path="path",
                dest="",
                dest_path="/dest",
                exclude=[".git/", "node_modules/"],
                return_command=True,
            )
            assert "--exclude .git/" in result
            assert "--exclude node_modules/" in result

        asyncio.run(_test())

    def test_copy_with_filter_patterns(self):
        """Copy command with filter patterns."""
        async def _test():
            result = await rclone_copy(
                rclone_config_path="/tmp/rclone.conf",
                source="remote",
                source_path="path",
                dest="",
                dest_path="/dest",
                filter=["+ *.py", "- *"],
                return_command=True,
            )
            assert "--filter '+ *.py'" in result
            assert "--filter '- *'" in result

        asyncio.run(_test())

    def test_copy_with_include_file(self):
        """Copy command with include-from file."""
        async def _test():
            result = await rclone_copy(
                rclone_config_path="/tmp/rclone.conf",
                source="remote",
                source_path="path",
                dest="",
                dest_path="/dest",
                include_file="/tmp/include.txt",
                return_command=True,
            )
            assert "--include-from /tmp/include.txt" in result

        asyncio.run(_test())

    def test_copy_with_exclude_file(self):
        """Copy command with exclude-from file."""
        async def _test():
            result = await rclone_copy(
                rclone_config_path="/tmp/rclone.conf",
                source="remote",
                source_path="path",
                dest="",
                dest_path="/dest",
                exclude_file="/tmp/exclude.txt",
                return_command=True,
            )
            assert "--exclude-from /tmp/exclude.txt" in result

        asyncio.run(_test())

    def test_copy_with_filters_file(self):
        """Copy command with filters-file."""
        async def _test():
            result = await rclone_copy(
                rclone_config_path="/tmp/rclone.conf",
                source="remote",
                source_path="path",
                dest="",
                dest_path="/dest",
                filters_file="/tmp/filters.txt",
                return_command=True,
            )
            assert "--filters-file /tmp/filters.txt" in result

        asyncio.run(_test())

    def test_copy_local_to_local(self):
        """Copy command with local paths (no remote prefix)."""
        async def _test():
            result = await rclone_copy(
                rclone_config_path="/tmp/rclone.conf",
                source="",
                source_path="/local/source",
                dest="",
                dest_path="/local/dest",
                return_command=True,
            )
            assert "/local/source" in result
            assert "/local/dest" in result
            # No colon prefixes for local paths
            assert ":/local" not in result

        asyncio.run(_test())


# ============================================================================
# Tests for rclone_copyto command building
# ============================================================================

# %%
#|export
class TestRcloneCopytoCommand:
    """Tests for rclone_copyto command building."""

    def test_basic_copyto_command(self):
        """Builds basic copyto command."""
        async def _test():
            result = await rclone_copyto(
                rclone_config_path="/tmp/rclone.conf",
                source="",
                source_path="/local/file.txt",
                dest="remote",
                dest_path="bucket/file.txt",
                return_command=True,
            )
            assert "rclone copyto" in result
            assert "--config /tmp/rclone.conf" in result
            assert "/local/file.txt" in result
            assert "remote:bucket/file.txt" in result

        asyncio.run(_test())

    def test_copyto_with_progress(self):
        """Copyto command with progress flag."""
        async def _test():
            result = await rclone_copyto(
                rclone_config_path="/tmp/rclone.conf",
                source="",
                source_path="/source",
                dest="",
                dest_path="/dest",
                progress=True,
                return_command=True,
            )
            assert "--progress" in result

        asyncio.run(_test())


# ============================================================================
# Tests for rclone_sync command building
# ============================================================================

# %%
#|export
class TestRcloneSyncCommand:
    """Tests for rclone_sync command building."""

    def test_basic_sync_command(self):
        """Builds basic sync command."""
        async def _test():
            result = await rclone_sync(
                rclone_config_path="/tmp/rclone.conf",
                source="",
                source_path="/local/path",
                dest="remote",
                dest_path="bucket/backup",
                return_command=True,
            )
            assert "rclone sync" in result
            assert "--config /tmp/rclone.conf" in result
            assert "/local/path" in result
            assert "remote:bucket/backup" in result

        asyncio.run(_test())

    def test_sync_with_backup_dir(self):
        """Sync command with backup directory."""
        async def _test():
            result = await rclone_sync(
                rclone_config_path="/tmp/rclone.conf",
                source="",
                source_path="/source",
                dest="",
                dest_path="/dest",
                backup_path="/backup/dir",
                return_command=True,
            )
            assert "--backup-dir /backup/dir" in result

        asyncio.run(_test())

    def test_sync_with_all_options(self):
        """Sync command with all options."""
        async def _test():
            result = await rclone_sync(
                rclone_config_path="/tmp/rclone.conf",
                source="src_remote",
                source_path="data",
                dest="dst_remote",
                dest_path="backup",
                include=["*.txt"],
                exclude=["*.tmp"],
                filter=["+ important/"],
                include_file="/inc.txt",
                exclude_file="/exc.txt",
                filters_file="/filters.txt",
                backup_path="/backup",
                dry_run=True,
                progress=True,
                return_command=True,
            )
            assert "--include '*.txt'" in result
            assert "--exclude '*.tmp'" in result
            assert "--filter '+ important/'" in result
            assert "--include-from /inc.txt" in result
            assert "--exclude-from /exc.txt" in result
            assert "--filters-file /filters.txt" in result
            assert "--backup-dir /backup" in result
            assert "--dry-run" in result
            assert "--progress" in result

        asyncio.run(_test())


# ============================================================================
# Tests for rclone_bisync command building
# ============================================================================

# %%
#|export
class TestRcloneBisyncCommand:
    """Tests for rclone_bisync command building."""

    def test_basic_bisync_command(self):
        """Builds basic bisync command."""
        async def _test():
            result = await rclone_bisync(
                rclone_config_path="/tmp/rclone.conf",
                source="",
                source_path="/local",
                dest="remote",
                dest_path="bucket",
                resync=False,
                force=False,
                return_command=True,
            )
            assert "rclone bisync" in result
            assert "--config /tmp/rclone.conf" in result

        asyncio.run(_test())

    def test_bisync_with_resync(self):
        """Bisync command with resync flag."""
        async def _test():
            result = await rclone_bisync(
                rclone_config_path="/tmp/rclone.conf",
                source="",
                source_path="/local",
                dest="remote",
                dest_path="bucket",
                resync=True,
                force=False,
                return_command=True,
            )
            assert "--resync" in result

        asyncio.run(_test())

    def test_bisync_with_force(self):
        """Bisync command with force flag."""
        async def _test():
            result = await rclone_bisync(
                rclone_config_path="/tmp/rclone.conf",
                source="",
                source_path="/local",
                dest="remote",
                dest_path="bucket",
                resync=False,
                force=True,
                return_command=True,
            )
            assert "--force" in result

        asyncio.run(_test())


# ============================================================================
# Tests for command execution with mocked run_cmd_async
# ============================================================================

# %%
#|export
class TestRcloneCommandExecution:
    """Tests for rclone command execution with mocked subprocess."""

    def test_copy_success(self):
        """rclone_copy returns True on success."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ):
                success, stdout, stderr = await rclone_copy(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/src",
                    dest="",
                    dest_path="/dst",
                )
            assert success is True

        asyncio.run(_test())

    def test_copy_failure(self):
        """rclone_copy returns False on failure."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(1, "", "error")),
            ):
                success, stdout, stderr = await rclone_copy(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/src",
                    dest="",
                    dest_path="/dst",
                )
            assert success is False

        asyncio.run(_test())

    def test_mkdir_raises_on_failure(self):
        """rclone_mkdir raises exception on failure."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(1, "", "mkdir failed")),
            ):
                with pytest.raises(Exception, match="mkdir failed"):
                    await rclone_mkdir(
                        rclone_config_path="/tmp/rclone.conf",
                        source="remote",
                        source_path="bucket/newdir",
                    )

        asyncio.run(_test())

    def test_lsjson_returns_parsed_json(self):
        """rclone_lsjson returns parsed JSON on success."""
        async def _test():
            mock_output = '[{"Name": "file.txt", "Size": 100}]'
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, mock_output, "")),
            ):
                result = await rclone_lsjson(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket",
                )
            assert result == [{"Name": "file.txt", "Size": 100}]

        asyncio.run(_test())

    def test_lsjson_returns_none_on_failure(self):
        """rclone_lsjson returns None on failure."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(1, "", "error")),
            ):
                result = await rclone_lsjson(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket",
                )
            assert result is None

        asyncio.run(_test())

    def test_path_exists_root(self):
        """rclone_path_exists returns True for root path."""
        async def _test():
            result = await rclone_path_exists(
                rclone_config_path="/tmp/rclone.conf",
                source="remote",
                source_path=".",
            )
            assert result == (True, True)

        asyncio.run(_test())

    def test_path_exists_found(self):
        """rclone_path_exists returns True when path exists."""
        async def _test():
            mock_output = '[{"Name": "mydir", "IsDir": true}]'
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, mock_output, "")),
            ):
                result = await rclone_path_exists(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/mydir",
                )
            assert result == (True, True)

        asyncio.run(_test())

    def test_path_exists_not_found(self):
        """rclone_path_exists returns False when path doesn't exist."""
        async def _test():
            mock_output = '[{"Name": "other", "IsDir": false}]'
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, mock_output, "")),
            ):
                result = await rclone_path_exists(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/missing",
                )
            assert result == (False, False)

        asyncio.run(_test())

    def test_purge_success(self):
        """rclone_purge returns True on success."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ):
                result = await rclone_purge(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/dir",
                )
            assert result is True

        asyncio.run(_test())

    def test_purge_failure(self):
        """rclone_purge returns False on failure."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(1, "", "error")),
            ):
                result = await rclone_purge(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/dir",
                )
            assert result is False

        asyncio.run(_test())

    def test_cat_success(self):
        """rclone_cat returns content on success."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "file content", "")),
            ):
                success, content = await rclone_cat(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/file.txt",
                )
            assert success is True
            assert content == "file content"

        asyncio.run(_test())

    def test_cat_failure(self):
        """rclone_cat returns None on failure."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(1, "", "error")),
            ):
                success, content = await rclone_cat(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/file.txt",
                )
            assert success is False
            assert content is None

        asyncio.run(_test())

    def test_move_success(self):
        """rclone_move returns True on success."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ):
                success, output = await rclone_move(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/src",
                    dest="",
                    dest_path="/dst",
                )
            assert success is True

        asyncio.run(_test())


# ============================================================================
# Tests for bisync result parsing
# ============================================================================

# %%
#|export
class TestBisyncResultParsing:
    """Tests for bisync result parsing."""

    def test_bisync_success(self):
        """Bisync returns SUCCESS on successful sync."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ):
                result, stdout, stderr = await rclone_bisync(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/local",
                    dest="remote",
                    dest_path="bucket",
                    resync=False,
                    force=False,
                )
            assert result == BisyncResult.SUCCESS

        asyncio.run(_test())

    def test_bisync_needs_resync(self):
        """Bisync returns ERROR_NEEDS_RESYNC on resync error."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(
                    return_value=(
                        1,
                        "",
                        "ERROR : Bisync aborted. Must run --resync to recover.",
                    )
                ),
            ):
                result, stdout, stderr = await rclone_bisync(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/local",
                    dest="remote",
                    dest_path="bucket",
                    resync=False,
                    force=False,
                )
            assert result == BisyncResult.ERROR_NEEDS_RESYNC

        asyncio.run(_test())

    def test_bisync_all_files_changed(self):
        """Bisync returns ERROR_ALL_FILES_CHANGED on safety abort."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(
                    return_value=(
                        1,
                        "",
                        "ERROR : Safety abort: all files were changed",
                    )
                ),
            ):
                result, stdout, stderr = await rclone_bisync(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/local",
                    dest="remote",
                    dest_path="bucket",
                    resync=False,
                    force=False,
                )
            assert result == BisyncResult.ERROR_ALL_FILES_CHANGED

        asyncio.run(_test())

    def test_bisync_conflicts(self):
        """Bisync returns CONFLICTS when conflicts detected."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(
                    return_value=(
                        0,
                        "",
                        "NOTICE: - WARNING  New or changed in both paths",
                    )
                ),
            ):
                result, stdout, stderr = await rclone_bisync(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/local",
                    dest="remote",
                    dest_path="bucket",
                    resync=False,
                    force=False,
                )
            assert result == BisyncResult.CONFLICTS

        asyncio.run(_test())

    def test_bisync_other_error(self):
        """Bisync returns ERROR_OTHER on other errors."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(1, "", "Some other error")),
            ):
                result, stdout, stderr = await rclone_bisync(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/local",
                    dest="remote",
                    dest_path="bucket",
                    resync=False,
                    force=False,
                )
            assert result == BisyncResult.ERROR_OTHER

        asyncio.run(_test())


# ============================================================================
# Tests for rclone_lsjson options
# ============================================================================

# %%
#|export
class TestRcloneLsjsonOptions:
    """Tests for rclone_lsjson command options."""

    def test_lsjson_with_options(self):
        """lsjson with various options."""
        async def _test():
            mock_output = "[]"
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, mock_output, "")),
            ):
                # Test dirs_only
                result = await rclone_lsjson(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket",
                    dirs_only=True,
                )
                assert result == []

                # Test files_only
                result = await rclone_lsjson(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket",
                    files_only=True,
                )
                assert result == []

                # Test recursive
                result = await rclone_lsjson(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket",
                    recursive=True,
                )
                assert result == []

                # Test max_depth
                result = await rclone_lsjson(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket",
                    max_depth=2,
                )
                assert result == []

                # Test filter
                result = await rclone_lsjson(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket",
                    filter=["+ *.py", "- *"],
                )
                assert result == []

        asyncio.run(_test())


# ============================================================================
# Tests for rclone_mkdir
# ============================================================================

# %%
#|export
from boxyard._utils import rclone_mkdir


class TestRcloneMkdir:
    """Tests for rclone_mkdir function."""

    def test_mkdir_builds_correct_command(self):
        """rclone_mkdir builds correct command."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ) as mock_run:
                await rclone_mkdir(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/newdir",
                )

                mock_run.assert_called_once()
                cmd = mock_run.call_args[0][0]
                assert cmd[0] == "rclone"
                assert cmd[1] == "mkdir"
                assert "--config" in cmd
                assert "/tmp/rclone.conf" in cmd
                assert "remote:bucket/newdir" in cmd

        asyncio.run(_test())

    def test_mkdir_local_path(self):
        """rclone_mkdir handles local paths (no source)."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ) as mock_run:
                await rclone_mkdir(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/local/newdir",
                )

                cmd = mock_run.call_args[0][0]
                assert "/local/newdir" in cmd
                assert ":" not in cmd[-1]  # No colon for local paths

        asyncio.run(_test())

    def test_mkdir_raises_on_failure(self):
        """rclone_mkdir raises exception on failure."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(1, "", "Permission denied")),
            ):
                with pytest.raises(Exception, match="Permission denied"):
                    await rclone_mkdir(
                        rclone_config_path="/tmp/rclone.conf",
                        source="remote",
                        source_path="bucket/newdir",
                    )

        asyncio.run(_test())


# ============================================================================
# Tests for rclone_path_exists
# ============================================================================

# %%
#|export
from boxyard._utils import rclone_path_exists


class TestRclonePathExists:
    """Tests for rclone_path_exists function."""

    def test_root_path_always_exists(self):
        """Root path '.' always returns (True, True)."""
        async def _test():
            result = await rclone_path_exists(
                rclone_config_path="/tmp/rclone.conf",
                source="remote",
                source_path=".",
            )

            assert result == (True, True)

        asyncio.run(_test())

    def test_path_exists_as_directory(self):
        """Returns (True, True) when path exists as directory."""
        async def _test():
            mock_ls_result = [
                {"Name": "mydir", "IsDir": True},
                {"Name": "file.txt", "IsDir": False},
            ]

            with patch(
                "boxyard._utils.rclone.rclone_lsjson",
                new=AsyncMock(return_value=mock_ls_result),
            ):
                result = await rclone_path_exists(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/mydir",
                )

            assert result == (True, True)

        asyncio.run(_test())

    def test_path_exists_as_file(self):
        """Returns (True, False) when path exists as file."""
        async def _test():
            mock_ls_result = [
                {"Name": "mydir", "IsDir": True},
                {"Name": "file.txt", "IsDir": False},
            ]

            with patch(
                "boxyard._utils.rclone.rclone_lsjson",
                new=AsyncMock(return_value=mock_ls_result),
            ):
                result = await rclone_path_exists(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/file.txt",
                )

            assert result == (True, False)

        asyncio.run(_test())

    def test_path_does_not_exist(self):
        """Returns (False, False) when path does not exist."""
        async def _test():
            mock_ls_result = [
                {"Name": "other.txt", "IsDir": False},
            ]

            with patch(
                "boxyard._utils.rclone.rclone_lsjson",
                new=AsyncMock(return_value=mock_ls_result),
            ):
                result = await rclone_path_exists(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/missing.txt",
                )

            assert result == (False, False)

        asyncio.run(_test())

    def test_parent_does_not_exist(self):
        """Returns (False, False) when parent directory doesn't exist."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.rclone_lsjson",
                new=AsyncMock(return_value=None),
            ):
                result = await rclone_path_exists(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="nonexistent/path/file.txt",
                )

            assert result == (False, False)

        asyncio.run(_test())


# ============================================================================
# Tests for rclone_purge
# ============================================================================

# %%
#|export
from boxyard._utils import rclone_purge


class TestRclonePurge:
    """Tests for rclone_purge function."""

    def test_purge_builds_correct_command(self):
        """rclone_purge builds correct command."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ) as mock_run:
                result = await rclone_purge(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/dir",
                )

                assert result is True
                mock_run.assert_called_once()
                cmd = mock_run.call_args[0][0]
                assert cmd[0] == "rclone"
                assert cmd[1] == "purge"
                assert "--config" in cmd
                assert "remote:bucket/dir" in cmd

        asyncio.run(_test())

    def test_purge_local_path(self):
        """rclone_purge handles local paths."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ) as mock_run:
                result = await rclone_purge(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/local/dir",
                )

                assert result is True
                cmd = mock_run.call_args[0][0]
                assert "/local/dir" in cmd

        asyncio.run(_test())

    def test_purge_returns_false_on_failure(self):
        """rclone_purge returns False on failure."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(1, "", "Directory not found")),
            ):
                result = await rclone_purge(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/missing",
                )

            assert result is False

        asyncio.run(_test())


# ============================================================================
# Tests for rclone_cat
# ============================================================================

# %%
#|export
from boxyard._utils import rclone_cat


class TestRcloneCat:
    """Tests for rclone_cat function."""

    def test_cat_builds_correct_command(self):
        """rclone_cat builds correct command."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "file contents", "")),
            ) as mock_run:
                success, content = await rclone_cat(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/file.txt",
                )

                assert success is True
                assert content == "file contents"
                cmd = mock_run.call_args[0][0]
                assert cmd[0] == "rclone"
                assert cmd[1] == "cat"
                assert "--config" in cmd
                assert "remote:bucket/file.txt" in cmd

        asyncio.run(_test())

    def test_cat_local_file(self):
        """rclone_cat handles local files."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "local content", "")),
            ) as mock_run:
                success, content = await rclone_cat(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/local/file.txt",
                )

                assert success is True
                assert content == "local content"
                cmd = mock_run.call_args[0][0]
                assert "/local/file.txt" in cmd

        asyncio.run(_test())

    def test_cat_returns_none_on_failure(self):
        """rclone_cat returns (False, None) on failure."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(1, "", "File not found")),
            ):
                success, content = await rclone_cat(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/missing.txt",
                )

            assert success is False
            assert content is None

        asyncio.run(_test())


# ============================================================================
# Tests for rclone_move
# ============================================================================

# %%
#|export
from boxyard._utils import rclone_move


class TestRcloneMove:
    """Tests for rclone_move function."""

    def test_move_builds_correct_command(self):
        """rclone_move builds correct command."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ) as mock_run:
                success, output = await rclone_move(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote1",
                    source_path="bucket1/file.txt",
                    dest="remote2",
                    dest_path="bucket2/file.txt",
                )

                assert success is True
                cmd = mock_run.call_args[0][0]
                assert cmd[0] == "rclone"
                assert cmd[1] == "move"
                assert "--config" in cmd
                assert "remote1:bucket1/file.txt" in cmd
                assert "remote2:bucket2/file.txt" in cmd

        asyncio.run(_test())

    def test_move_local_to_remote(self):
        """rclone_move handles local to remote move."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ) as mock_run:
                success, output = await rclone_move(
                    rclone_config_path="/tmp/rclone.conf",
                    source="",
                    source_path="/local/file.txt",
                    dest="remote",
                    dest_path="bucket/file.txt",
                )

                assert success is True
                cmd = mock_run.call_args[0][0]
                assert "/local/file.txt" in cmd
                assert "remote:bucket/file.txt" in cmd

        asyncio.run(_test())

    def test_move_remote_to_local(self):
        """rclone_move handles remote to local move."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(0, "", "")),
            ) as mock_run:
                success, output = await rclone_move(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/file.txt",
                    dest="",
                    dest_path="/local/file.txt",
                )

                assert success is True
                cmd = mock_run.call_args[0][0]
                assert "remote:bucket/file.txt" in cmd
                assert "/local/file.txt" in cmd

        asyncio.run(_test())

    def test_move_returns_stderr_on_failure(self):
        """rclone_move returns (False, stderr) on failure."""
        async def _test():
            with patch(
                "boxyard._utils.rclone.run_cmd_async",
                new=AsyncMock(return_value=(1, "", "Permission denied")),
            ):
                success, output = await rclone_move(
                    rclone_config_path="/tmp/rclone.conf",
                    source="remote",
                    source_path="bucket/file.txt",
                    dest="remote2",
                    dest_path="bucket2/file.txt",
                )

            assert success is False
            assert output == "Permission denied"

        asyncio.run(_test())

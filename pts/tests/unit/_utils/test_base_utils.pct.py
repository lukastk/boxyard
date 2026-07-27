# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for Base Utilities

# %%
#|default_exp unit._utils.test_base_utils

# %%
#|export
import pytest
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock
import time


# ============================================================================
# Tests for get_box_index_name_from_sub_path
# ============================================================================

# %%
#|export
from boxyard._utils import get_box_index_name_from_sub_path


class TestGetBoxIndexNameFromSubPath:
    """Tests for get_box_index_name_from_sub_path function."""

    @pytest.fixture
    def mock_config(self, tmp_path):
        """Create a mock config with user_boxes_path."""
        config = MagicMock()
        config.user_boxes_path = tmp_path / "boxes"
        config.user_boxes_path.mkdir(parents=True, exist_ok=True)
        return config

    def test_path_inside_box(self, mock_config):
        """Returns index_name for path inside a box."""
        # Create box directory structure
        box_path = mock_config.user_boxes_path / "20240101_120000_abcde__mybox"
        box_path.mkdir(parents=True, exist_ok=True)
        sub_path = box_path / "src" / "main.py"
        sub_path.parent.mkdir(parents=True, exist_ok=True)
        sub_path.touch()

        result = get_box_index_name_from_sub_path(mock_config, str(sub_path))

        assert result == "20240101_120000_abcde__mybox"

    def test_path_at_box_root(self, mock_config):
        """Returns index_name for path at box root."""
        box_path = mock_config.user_boxes_path / "20240101_120000_abcde__mybox"
        box_path.mkdir(parents=True, exist_ok=True)

        result = get_box_index_name_from_sub_path(mock_config, str(box_path))

        assert result == "20240101_120000_abcde__mybox"

    def test_path_outside_boxes(self, mock_config, tmp_path):
        """Returns None for path outside user_boxes_path."""
        outside_path = tmp_path / "other" / "file.py"

        result = get_box_index_name_from_sub_path(mock_config, str(outside_path))

        assert result is None

    def test_path_at_boxes_root(self, mock_config):
        """Returns None for path at user_boxes_path root itself."""
        result = get_box_index_name_from_sub_path(
            mock_config, str(mock_config.user_boxes_path)
        )

        assert result is None

    def test_path_with_tilde(self, mock_config):
        """Handles paths with tilde expansion."""
        # Create box under home-like structure
        box_path = mock_config.user_boxes_path / "20240101_120000_abcde__mybox"
        box_path.mkdir(parents=True, exist_ok=True)

        result = get_box_index_name_from_sub_path(mock_config, str(box_path))

        assert result == "20240101_120000_abcde__mybox"


# ============================================================================
# Tests for get_hostname
# ============================================================================

# %%
#|export
from boxyard._utils import get_hostname


class TestGetHostname:
    """Tests for get_hostname function."""

    def test_returns_string(self):
        """get_hostname returns a string."""
        result = get_hostname()

        assert isinstance(result, str)
        assert len(result) > 0

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.run")
    def test_darwin_uses_scutil(self, mock_run, mock_system):
        """On Darwin, tries scutil first."""
        mock_run.return_value = MagicMock(
            stdout="MyMacBook\n",
            returncode=0,
        )

        result = get_hostname()

        mock_run.assert_called_once()
        assert "scutil" in mock_run.call_args[0][0]
        assert result == "MyMacBook"

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.run", side_effect=Exception("scutil failed"))
    @patch("platform.node", return_value="fallback-host")
    def test_darwin_fallback_to_platform_node(
        self, mock_node, mock_run, mock_system
    ):
        """On Darwin, falls back to platform.node if scutil fails."""
        result = get_hostname()

        assert result == "fallback-host"

    @patch("platform.system", return_value="Linux")
    @patch("platform.node", return_value="linux-host")
    def test_linux_uses_platform_node(self, mock_node, mock_system):
        """On Linux, uses platform.node directly."""
        result = get_hostname()

        assert result == "linux-host"


# ============================================================================
# Tests for check_last_time_modified
# ============================================================================

# %%
#|export
from boxyard._utils import check_last_time_modified


class TestCheckLastTimeModified:
    """Tests for check_last_time_modified function."""

    def test_single_file(self, tmp_path):
        """Returns modification time for a single file."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        result = check_last_time_modified(test_file)

        assert result is not None
        assert isinstance(result, datetime)
        assert result.tzinfo == timezone.utc

    def test_directory_with_files(self, tmp_path):
        """Returns latest modification time in directory."""
        # Create files with different mtimes
        file1 = tmp_path / "file1.txt"
        file1.write_text("content1")

        time.sleep(0.1)  # Ensure different timestamps

        file2 = tmp_path / "file2.txt"
        file2.write_text("content2")

        result = check_last_time_modified(tmp_path)

        assert result is not None
        # Result should be close to file2's mtime (the newer one)
        file2_mtime = datetime.fromtimestamp(file2.stat().st_mtime, tz=timezone.utc)
        assert abs((result - file2_mtime).total_seconds()) < 1

    def test_nested_directory(self, tmp_path):
        """Handles nested directories."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        nested_file = subdir / "nested.txt"
        nested_file.write_text("content")

        result = check_last_time_modified(tmp_path)

        assert result is not None

    def test_empty_directory(self, tmp_path):
        """Returns None for empty directory."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        result = check_last_time_modified(empty_dir)

        assert result is None

    def test_nonexistent_path(self, tmp_path):
        """Returns None for nonexistent path."""
        nonexistent = tmp_path / "nonexistent"

        result = check_last_time_modified(nonexistent)

        assert result is None

    def test_path_with_tilde(self, tmp_path):
        """Handles paths with tilde expansion."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        # Test with string path
        result = check_last_time_modified(str(test_file))

        assert result is not None


# ============================================================================
# Tests for run_cmd_async
# ============================================================================

# %%
#|export
from boxyard._utils import run_cmd_async


class TestRunCmdAsync:
    """Tests for run_cmd_async function."""

    def test_successful_command(self):
        """Runs successful command and captures output."""
        async def _test():
            returncode, stdout, stderr = await run_cmd_async(["echo", "hello"])
            assert returncode == 0
            assert "hello" in stdout
            assert stderr == ""

        asyncio.run(_test())

    def test_command_with_stderr(self):
        """Captures stderr output."""
        async def _test():
            returncode, stdout, stderr = await run_cmd_async(
                ["python", "-c", "import sys; sys.stderr.write('error\\n')"]
            )
            assert returncode == 0
            assert "error" in stderr

        asyncio.run(_test())

    def test_command_failure(self):
        """Handles command failure."""
        async def _test():
            returncode, stdout, stderr = await run_cmd_async(
                ["python", "-c", "import sys; sys.exit(1)"]
            )
            assert returncode == 1

        asyncio.run(_test())

    def test_command_with_arguments(self):
        """Handles commands with multiple arguments."""
        async def _test():
            returncode, stdout, stderr = await run_cmd_async(
                ["python", "-c", "print('arg1', 'arg2')"]
            )
            assert returncode == 0
            assert "arg1" in stdout
            assert "arg2" in stdout

        asyncio.run(_test())


# ============================================================================
# Tests for the run_cmd_async timeout and suspend watchdog
# ============================================================================

# %%
#|export
import os
import signal

from boxyard._utils.base import (
    CommandTimeout,
    SuspendInterruption,
    _live_procs,
    _suspend_watchdog_loop,
)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class TestRunCmdAsyncTimeout:
    """A bounded command that overruns must be killed, not waited on forever."""

    def test_timeout_raises(self):
        """Exceeding the timeout raises CommandTimeout rather than hanging."""
        async def _test():
            with pytest.raises(CommandTimeout):
                await run_cmd_async(
                    ["python", "-c", "import time; time.sleep(30)"], timeout=0.5
                )

        asyncio.run(_test())

    def test_timeout_leaves_no_orphan(self):
        """The timed-out process is actually dead once the error surfaces."""
        pids = []

        async def _test():
            # Record the pid so we can check it after the kill.
            orig = asyncio.create_subprocess_exec

            async def _spy(*args, **kwargs):
                proc = await orig(*args, **kwargs)
                pids.append(proc.pid)
                return proc

            with patch("asyncio.create_subprocess_exec", _spy):
                with pytest.raises(CommandTimeout):
                    await run_cmd_async(
                        ["python", "-c", "import time; time.sleep(30)"], timeout=0.5
                    )

        asyncio.run(_test())
        assert len(pids) == 1
        assert not _pid_is_alive(pids[0])

    def test_timeout_kills_child_processes(self):
        """The whole process group dies, not just the process we spawned."""
        marker = Path(tempfile.gettempdir()) / f"boxyard_pgkill_{os.getpid()}.txt"
        marker.unlink(missing_ok=True)

        # Parent spawns a child that would write the marker after we give up on
        # the parent. If only the parent were killed, the child would survive
        # and the marker would appear.
        script = (
            "import subprocess, time; "
            f"subprocess.Popen(['python','-c',\"import time; time.sleep(2); "
            f"open({str(marker)!r},'w').write('orphan')\"]); "
            "time.sleep(30)"
        )

        async def _test():
            with pytest.raises(CommandTimeout):
                await run_cmd_async(["python", "-c", script], timeout=0.5)

        asyncio.run(_test())
        time.sleep(3)
        assert not marker.exists(), "child survived the process-group kill"

    def test_no_timeout_still_waits(self):
        """timeout=None keeps the old unbounded behaviour for transfers."""
        async def _test():
            returncode, stdout, _ = await run_cmd_async(
                ["python", "-c", "import time; time.sleep(1); print('done')"],
                timeout=None,
            )
            assert returncode == 0
            assert "done" in stdout

        asyncio.run(_test())


class TestSuspendWatchdog:
    """
    A wall-clock jump that the monotonic clock does not match means the machine
    was suspended, so every live child is holding a dead connection.
    """

    @staticmethod
    def _fake_wall_clock():
        """
        A `time.time` stand-in whose offset the test can move at will.

        The offset has to change *between* the watchdog's before- and after-sleep
        samples to look like a suspend. A constant offset cancels out in the
        subtraction and simulates nothing.
        """
        offset = {"seconds": 0.0}
        real_time = time.time
        return offset, lambda: real_time() + offset["seconds"]

    async def _spawn_sleeper(self, seconds: int = 30):
        return await asyncio.create_subprocess_exec(
            "python", "-c", f"import time; time.sleep({seconds})",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    def test_detects_clock_divergence_and_kills(self):
        """A simulated suspend kills the in-flight child with SIGKILL."""
        async def _test():
            proc = await self._spawn_sleeper()
            _live_procs.add(proc)
            offset, fake_time = self._fake_wall_clock()
            try:
                with patch("boxyard.const.SUSPEND_POLL_INTERVAL", 0.05):
                    with patch("time.time", fake_time):
                        watchdog = asyncio.create_task(_suspend_watchdog_loop())
                        await asyncio.sleep(0.2)
                        # Clocks still in step, so nothing should have died yet.
                        assert proc.returncode is None

                        offset["seconds"] = 3600  # the machine "sleeps" an hour
                        try:
                            # Fails loudly if the watchdog never fires, rather
                            # than quietly waiting out the sleeper.
                            await asyncio.wait_for(proc.wait(), timeout=5)
                        finally:
                            watchdog.cancel()

                assert proc.returncode == -signal.SIGKILL
            finally:
                _live_procs.discard(proc)

        asyncio.run(_test())

    def test_normal_running_is_left_alone(self):
        """With clocks in step, the watchdog never touches a live child."""
        async def _test():
            proc = await self._spawn_sleeper(seconds=5)
            _live_procs.add(proc)
            try:
                with patch("boxyard.const.SUSPEND_POLL_INTERVAL", 0.05):
                    watchdog = asyncio.create_task(_suspend_watchdog_loop())
                    await asyncio.sleep(0.4)
                    watchdog.cancel()
                assert proc.returncode is None
            finally:
                _live_procs.discard(proc)
                proc.kill()
                await proc.wait()

        asyncio.run(_test())

    def test_suspend_kill_surfaces_as_error(self):
        """A child killed by the watchdog raises instead of looking successful."""
        async def _test():
            offset, fake_time = self._fake_wall_clock()

            async def _simulate_suspend():
                await asyncio.sleep(0.2)
                offset["seconds"] = 3600

            with patch("boxyard.const.SUSPEND_POLL_INTERVAL", 0.05):
                with patch("time.time", fake_time):
                    suspend = asyncio.create_task(_simulate_suspend())
                    try:
                        with pytest.raises(SuspendInterruption):
                            await run_cmd_async(
                                ["python", "-c", "import time; time.sleep(30)"]
                            )
                    finally:
                        suspend.cancel()

        asyncio.run(_test())


# ============================================================================
# Tests for async_throttler
# ============================================================================

# %%
#|export
from boxyard._utils import async_throttler


class TestAsyncThrottler:
    """Tests for async_throttler function."""

    def test_runs_all_coroutines(self):
        """Runs all coroutines and returns results."""
        async def _test():
            async def simple_coro(x):
                return x * 2

            coros = [simple_coro(i) for i in range(5)]
            results = await async_throttler(coros, max_concurrency=2)
            assert results == [0, 2, 4, 6, 8]

        asyncio.run(_test())

    def test_respects_max_concurrency(self):
        """Respects max concurrency limit."""
        async def _test():
            concurrent_count = 0
            max_concurrent = 0

            async def tracking_coro(x):
                nonlocal concurrent_count, max_concurrent
                concurrent_count += 1
                max_concurrent = max(max_concurrent, concurrent_count)
                await asyncio.sleep(0.01)
                concurrent_count -= 1
                return x

            coros = [tracking_coro(i) for i in range(10)]
            await async_throttler(coros, max_concurrency=3)
            assert max_concurrent <= 3

        asyncio.run(_test())

    def test_handles_timeout(self):
        """Handles timeout for slow coroutines."""
        async def _test():
            async def slow_coro():
                await asyncio.sleep(10)
                return "done"

            coros = [slow_coro()]

            with pytest.raises(asyncio.TimeoutError):
                await async_throttler(coros, max_concurrency=1, timeout=0.01)

        asyncio.run(_test())

    def test_raises_on_exception(self):
        """Raises exception from failed coroutine."""
        async def _test():
            async def failing_coro():
                raise ValueError("test error")

            coros = [failing_coro()]

            with pytest.raises(ValueError, match="test error"):
                await async_throttler(coros, max_concurrency=1)

        asyncio.run(_test())

    def test_empty_list(self):
        """Handles empty coroutine list."""
        async def _test():
            results = await async_throttler([], max_concurrency=5)
            assert results == []

        asyncio.run(_test())


# ============================================================================
# Tests for is_in_event_loop
# ============================================================================

# %%
#|export
from boxyard._utils import is_in_event_loop


class TestIsInEventLoop:
    """Tests for is_in_event_loop function."""

    def test_not_in_event_loop(self):
        """Returns False when not in event loop."""
        # When running in pytest without asyncio marker
        result = is_in_event_loop()

        assert result is False

    def test_in_event_loop(self):
        """Returns True when in event loop."""
        async def _test():
            result = is_in_event_loop()
            assert result is True

        asyncio.run(_test())


# ============================================================================
# Tests for count_files_in_dir
# ============================================================================

# %%
#|export
from boxyard._utils import count_files_in_dir


class TestCountFilesInDir:
    """Tests for count_files_in_dir function."""

    def test_empty_directory(self, tmp_path):
        """Returns 0 for empty directory."""
        result = count_files_in_dir(tmp_path)

        assert result == 0

    def test_flat_directory(self, tmp_path):
        """Counts files in flat directory."""
        (tmp_path / "file1.txt").touch()
        (tmp_path / "file2.txt").touch()
        (tmp_path / "file3.txt").touch()

        result = count_files_in_dir(tmp_path)

        assert result == 3

    def test_nested_directory(self, tmp_path):
        """Counts files in nested directories."""
        (tmp_path / "file1.txt").touch()
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "file2.txt").touch()
        (subdir / "file3.txt").touch()
        deep_subdir = subdir / "deep"
        deep_subdir.mkdir()
        (deep_subdir / "file4.txt").touch()

        result = count_files_in_dir(tmp_path)

        assert result == 4

    def test_ignores_subdirectories(self, tmp_path):
        """Only counts files, not directories."""
        (tmp_path / "file.txt").touch()
        (tmp_path / "subdir1").mkdir()
        (tmp_path / "subdir2").mkdir()

        result = count_files_in_dir(tmp_path)

        assert result == 1


# ============================================================================
# Tests for SoftInterruption
# ============================================================================

# %%
#|export
from boxyard._utils import SoftInterruption


class TestSoftInterruption:
    """Tests for SoftInterruption exception class."""

    def test_is_exception(self):
        """SoftInterruption is an Exception."""
        assert issubclass(SoftInterruption, Exception)

    def test_can_be_raised(self):
        """SoftInterruption can be raised and caught."""
        with pytest.raises(SoftInterruption):
            raise SoftInterruption()

    def test_with_message(self):
        """SoftInterruption can have a message."""
        with pytest.raises(SoftInterruption, match="custom message"):
            raise SoftInterruption("custom message")


# ============================================================================
# Tests for enable_soft_interruption and check_interrupted
# ============================================================================

# %%
#|export
from boxyard._utils import enable_soft_interruption, check_interrupted
import boxyard._utils.base as base_module


class TestSoftInterruptionHandling:
    """Tests for soft interruption handling."""

    def test_check_interrupted_initial_state(self):
        """check_interrupted returns False initially after reset."""
        # Reset the global state
        base_module._interrupted = False
        base_module._interrupt_count = 0

        result = check_interrupted()

        assert result is False

    def test_interrupted_flag_can_be_set(self):
        """_interrupted flag can be set."""
        base_module._interrupted = True

        result = check_interrupted()

        assert result is True

        # Cleanup
        base_module._interrupted = False

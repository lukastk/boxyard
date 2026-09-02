# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _utils.base

# %%
#|default_exp _utils.base

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();
import boxyard._utils.base as this_module

# %%
#|export
import os
import signal
import subprocess
import asyncio
import time
from boxyard import const
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

import boxyard.config

# %%
#|hide
show_doc(this_module.get_box_index_name_from_sub_path)

# %%
#|export
def get_box_index_name_from_sub_path(
    config: boxyard.config.Config,
    sub_path: str,
 ) -> str | None:
    """Return the first checkout path component containing ``sub_path``.

    Both the candidate and roots are resolved, so a path reached through a
    group symlink is identified by its physical checkout root. Longest roots
    are tried first; overlapping roots are separately rejected for mutations
    and reported by doctor.
    """
    candidate = Path(sub_path).expanduser().resolve(strict=False)
    roots = sorted(
        config.configured_checkout_roots.values(),
        key=lambda root: len(root.path.resolve(strict=False).parts),
        reverse=True,
    )
    for root in roots:
        resolved_root = root.path.resolve(strict=False)
        try:
            relative = candidate.relative_to(resolved_root)
        except ValueError:
            continue
        if not relative.parts:
            return None
        return relative.parts[0]
    return None

# %%
#|hide
show_doc(this_module.get_hostname)

# %%
#|export
import platform


def get_hostname():
    system = platform.system()
    hostname = None
    if system == "Darwin":
        # Mac
        try:
            result = subprocess.run(
                ["scutil", "--get", "ComputerName"],
                capture_output=True,
                text=True,
                check=True,
            )
            hostname = result.stdout.strip()
        except Exception:
            hostname = None
    if hostname is None:
        hostname = platform.node()
    return hostname

# %%
#|exporti
def _reject_ambiguous_disp_terms(terms: list[str], disp_terms: list[str]) -> None:
    """
    Refuse a picker whose display lines cannot be mapped back to one term.

    fzf returns the LINE the user chose, and both wrappers map that back to a
    term with `disp_terms.index(...)` -- which returns the FIRST match. Two
    items rendering to the same line therefore resolve to the same term, so
    picking the second one silently acts on the first. For a picker that feeds
    `boxyard exclude`, that is the wrong box being removed from the machine.

    Every caller today embeds the box id in its display line, so this cannot
    fire; it exists so that a caller which forgets gets a loud error instead of
    a wrong box. Lines are compared after `.strip()`, because that is what the
    lookup does.

    Mismatched list lengths are refused for the same reason: the mapping is
    positional, so a short `terms` would resolve a high index to the wrong item
    or raise an opaque IndexError deep inside the wrapper.
    """
    if len(terms) != len(disp_terms):
        raise ValueError(
            f"run_fzf: {len(terms)} terms but {len(disp_terms)} display terms; "
            f"the two are mapped positionally and must be the same length."
        )
    stripped = [t.strip() for t in disp_terms]
    seen = set()
    for term in stripped:
        if term in seen:
            raise ValueError(
                f"run_fzf: duplicate display term {term!r}. fzf returns the "
                f"chosen LINE, so duplicates cannot be mapped back to a single "
                f"item and picking one would act on another. Include something "
                f"unique (the box id) in each display term."
            )
        seen.add(term)

# %%
#|hide
show_doc(this_module.run_fzf)

# %%
#|export
def run_fzf(terms: list[str], disp_terms: list[str] | None = None):
    """
    Launches the fzf command-line fuzzy finder with a list of terms and returns
    the selected term.

    Parameters:
    terms (List[str]): A list of strings to be presented to fzf for selection.

    Returns:
    str or None: The selected string from fzf, or None if no selection was made
    or if fzf encountered an error.

    Raises:
    RuntimeError: If fzf is not installed or not found in the system PATH.
    """
    import subprocess

    if disp_terms is None:
        disp_terms = terms
    _reject_ambiguous_disp_terms(terms, disp_terms)
    try:
        # Launch fzf with the list of strings
        result = subprocess.run(
            ["fzf"], input="\n".join(disp_terms), text=True, capture_output=True
        )
        if result.returncode != 0:
            return None, None
        res_term = result.stdout.strip()
        term_index = [t.strip() for t in disp_terms].index(res_term)
        sel_term = terms[term_index]
        return term_index, sel_term
    except FileNotFoundError:
        raise RuntimeError("fzf is not installed or not found in PATH.")

# %%
#|hide
show_doc(this_module.run_fzf_multi)

# %%
#|export
def run_fzf_multi(terms: list[str], disp_terms: list[str] | None = None):
    """
    Launches fzf in multi-select mode and returns all selected terms.

    Use Tab or Space to toggle selection, Enter to confirm.

    Parameters:
    terms (List[str]): A list of strings to be presented to fzf for selection.
    disp_terms (List[str] | None): Optional display strings for each term.

    Returns:
    list[tuple[int, str]]: List of (index, term) tuples for each selected item,
        or an empty list if no selection was made.

    Raises:
    RuntimeError: If fzf is not installed or not found in the system PATH.
    """
    import subprocess

    if disp_terms is None:
        disp_terms = terms
    _reject_ambiguous_disp_terms(terms, disp_terms)
    try:
        result = subprocess.run(
            ["fzf", "--multi"], input="\n".join(disp_terms), text=True, capture_output=True
        )
        if result.returncode != 0:
            return []
        selected_lines = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
        disp_stripped = [t.strip() for t in disp_terms]
        selections = []
        for sel in selected_lines:
            idx = disp_stripped.index(sel)
            selections.append((idx, terms[idx]))
        return selections
    except FileNotFoundError:
        raise RuntimeError("fzf is not installed or not found in PATH.")

# %%
#|hide
show_doc(this_module.check_last_time_modified)

# %%
#|export
def literal_exclude_names(exclude_path: "str | Path | None") -> set[str]:
    """
    Read an rclone exclude file and return the patterns that are LITERAL names.

    Only literal names are returned -- `node_modules/`, `.DS_Store` -- with any
    trailing slash stripped. Glob patterns (`*.tmp`, `**/build`) are deliberately
    NOT interpreted: reimplementing rclone's filter language here would be a
    second, subtly-different implementation of the thing that actually decides
    what gets transferred.

    The consequence is a one-sided inaccuracy, which is the safe side to err on:
    a glob-excluded file can still make a box look modified (a false "needs
    push", which sync then resolves as a no-op), but nothing that WOULD be
    synced is ever skipped.
    """
    if exclude_path is None:
        return set()
    try:
        text = Path(exclude_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if any(ch in line for ch in "*?[]{}") or "/" in line.rstrip("/"):
            continue  # a glob or a path pattern -- not a literal name
        names.add(line.rstrip("/"))
    return names


def check_last_time_modified(
    path: "str | Path",
    exclude_names: "set[str] | None" = None,
) -> "datetime | None":
    """
    Return the most recent modification time beneath `path`, or None if there is
    nothing to measure.

    `exclude_names` are literal file/directory names to skip -- normally derived
    from the box's effective rclone exclude file via `literal_exclude_names`.

    Skipping them matters because this answer drives the sync decision. Without
    it, a file that can NEVER be transferred still marks the box as locally
    modified: macOS Finder writing a `.DS_Store` was enough to flip a box to
    `NEEDS_PUSH`, and -- when the remote had also moved on -- to `CONFLICT`.
    That is exactly how a box ends up wedged with no real changes on one side.

    The patterns must be the box's OWN effective excludes, not a hardcoded list:
    a per-box `conf/.rclone_exclude` REPLACES the global default, so assuming
    the defaults for a box that overrides them could skip a directory the box
    really does sync -- which would hide genuine changes.
    """
    import os
    from datetime import timezone

    exclude_names = exclude_names or set()

    path = Path(path).expanduser().resolve()

    if path.is_file():
        if path.name in exclude_names:
            return None
        max_mtime = path.stat().st_mtime
    else:
        max_mtime = None
        stack = [str(path)]

        while stack:
            current = stack.pop()
            # A directory we cannot read must NOT be skipped silently.
            #
            # This walk answers "when did this box last change?", and the answer
            # drives the sync decision. Swallowing a permission or I/O error
            # here lowers the reported mtime, so a box with real local changes
            # underneath an unreadable directory looks SYNCED and is never
            # pushed -- data loss by omission, with no error anywhere. The same
            # failure mode was fixed in `_utils/perms.py`, where a swallowed
            # walk error silently shrank the permissions manifest.
            #
            # A directory that VANISHES mid-walk is different: that race is real
            # and legitimate (a build directory being cleaned, a temp file
            # going away), so it is tolerated.
            try:
                entries = list(os.scandir(current))
            except FileNotFoundError:
                continue
            except OSError as e:
                raise OSError(
                    f"Cannot determine when '{path}' last changed: "
                    f"'{current}' could not be read ({e}). Fix the permissions, "
                    f"or exclude it from the box."
                ) from e

            for entry in entries:
                if entry.name in exclude_names:
                    continue
                if entry.is_file(follow_symlinks=False):
                    try:
                        stat_result = entry.stat()
                    except FileNotFoundError:
                        # The file went away between scandir and stat.
                        continue
                    mtime = stat_result.st_mtime
                    if max_mtime is None or mtime > max_mtime:
                        max_mtime = mtime
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)

    return (
        datetime.fromtimestamp(max_mtime, tz=timezone.utc)
        if max_mtime is not None
        else None
    )

# %%
#|hide
show_doc(this_module.run_cmd_async)

# %%
#|export
# Semaphore to limit concurrent subprocess creation and avoid fd exhaustion
_subprocess_semaphore: asyncio.Semaphore | None = None
_MAX_CONCURRENT_SUBPROCESSES = 10


def _get_subprocess_semaphore() -> asyncio.Semaphore:
    """Get or create the subprocess semaphore for the current event loop."""
    global _subprocess_semaphore
    if _subprocess_semaphore is None:
        _subprocess_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SUBPROCESSES)
    return _subprocess_semaphore


class CommandTimeout(Exception):
    """A subprocess exceeded its timeout and was killed."""


class SuspendInterruption(Exception):
    """A subprocess was killed because the machine resumed from sleep."""


# Subprocesses currently running, so the suspend watchdog can kill them all when
# the machine wakes up, plus the pids it killed, so each `run_cmd_async` can tell
# "died because we killed it" from "exited on its own".
_live_procs: set = set()
_suspend_killed_pids: set[int] = set()
_suspend_watchdog: tuple[asyncio.AbstractEventLoop, asyncio.Task] | None = None


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill `proc` and anything it spawned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Already reaped, or not in a group of ours — kill the process itself.
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _suspend_watchdog_loop() -> None:
    """
    Kill in-flight subprocesses whenever the machine resumes from sleep.

    `time.monotonic()` does not advance while the system is suspended but
    `time.time()` does, so a divergence between the two means the machine was
    asleep — and every TCP connection held by a live rclone child is now dead.

    rclone does not reliably notice this. Its own `--timeout` (5m IO idle),
    `--contimeout` (1m) and `--sftp-idle-timeout` (1m) were all in effect when
    two `lsjson` processes, spawned two seconds before an idle sleep, span at
    100% CPU with no open sockets for 9.5 hours. Killing them here lets the
    caller fail loudly and retry on fresh connections.
    """
    while True:
        wall_before, mono_before = time.time(), time.monotonic()
        await asyncio.sleep(const.SUSPEND_POLL_INTERVAL)
        slept_for = (time.time() - wall_before) - (time.monotonic() - mono_before)

        if slept_for < const.SUSPEND_DETECT_THRESHOLD:
            continue

        for proc in list(_live_procs):
            _suspend_killed_pids.add(proc.pid)
            _kill_process_group(proc)


def _ensure_suspend_watchdog() -> None:
    """Start the suspend watchdog for the running loop, if it isn't already up."""
    global _suspend_watchdog
    loop = asyncio.get_running_loop()

    if _suspend_watchdog is not None:
        watchdog_loop, task = _suspend_watchdog
        if watchdog_loop is loop and not task.done():
            return

    _suspend_watchdog = (loop, loop.create_task(_suspend_watchdog_loop()))


async def _stream_stdout(proc, callback) -> tuple[bytes, bytes]:
    """
    Feed each stdout line to `callback` and keep none of it.

    stderr is drained CONCURRENTLY. Reading stdout to completion first would
    deadlock the moment a child filled its stderr pipe -- and restic writes
    warnings there while it works.
    """
    stderr_task = asyncio.create_task(proc.stderr.read())
    try:
        async for raw in proc.stdout:
            callback(raw.decode("utf-8", "replace").rstrip("\n"))
    finally:
        stderr = await stderr_task
    await proc.wait()
    return b"", stderr


async def run_cmd_async(
    cmd: list[str],
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    stdout_line_callback: "Callable[[str], None] | None" = None,
) -> tuple[int, str, str]:
    """
    Run `cmd`, returning `(returncode, stdout, stderr)`.

    `timeout` is a wall-clock ceiling, and suits only operations whose work is
    inherently bounded — listings and metadata reads. Transfers have no
    meaningful upper bound (a big box legitimately takes hours) and should be
    left unbounded; the suspend watchdog covers them instead.

    `env` REPLACES the child's environment when given; `None` inherits this
    process's, which is what every caller before restic wanted. It exists
    because restic takes its repository password through `RESTIC_PASSWORD`, and
    the alternatives are worse: `--password-command` reruns a 1Password fetch on
    every invocation, and mutating `os.environ` would race with the other
    coroutines this function deliberately runs concurrently.

    `stdout_line_callback` STREAMS stdout: each line is handed to the callback
    as it arrives and nothing is accumulated, so the returned stdout is empty.
    `None` (the default) buffers as before, which is what every caller but one
    wants.

    It exists because restic's change detection lists a whole snapshot's nodes
    (`restic ls --json --long`) to compare them against the tree, and one real
    box holds 688,000 files. That output is read once, node by node, to build a
    map -- so buffering the raw text as well would double the peak for nothing.
    Measured on the same shape: a 7,260-node tree emits 1.6 MB.

    Raises `CommandTimeout` if `timeout` is exceeded, or `SuspendInterruption`
    if the machine resumed from sleep mid-run. Both kill the whole process
    group first, so no orphans are left behind.
    """
    _ensure_suspend_watchdog()
    semaphore = _get_subprocess_semaphore()
    async with semaphore:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group, so we can take its children down with it.
            start_new_session=True,
            env=env,
        )
        _live_procs.add(proc)
        try:
            try:
                if stdout_line_callback is None:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout
                    )
                else:
                    stdout, stderr = await asyncio.wait_for(
                        _stream_stdout(proc, stdout_line_callback), timeout
                    )
            except (asyncio.TimeoutError, TimeoutError):
                _kill_process_group(proc)
                await proc.wait()
                raise CommandTimeout(
                    f"`{' '.join(cmd[:2])}` exceeded its {timeout:g}s timeout "
                    f"and was killed."
                ) from None

            if proc.pid in _suspend_killed_pids:
                raise SuspendInterruption(
                    f"`{' '.join(cmd[:2])}` was killed because the machine "
                    f"resumed from sleep; its connections were dead."
                )

            return proc.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")
        finally:
            _live_procs.discard(proc)
            _suspend_killed_pids.discard(proc.pid)

# %%
await run_cmd_async(["echo", "hello", "world"])

# %%
#|hide
show_doc(this_module.async_throttler)

# %%
#|export
async def async_throttler(
    coros: list[Coroutine],
    max_concurrency: int,
    timeout: float | None = None,
) -> list[Any]:
    """
    Throttle a list of coroutines to run concurrently.
    """

    sem = asyncio.Semaphore(max_concurrency)

    async def _task(coro: Coroutine) -> Any:
        async with sem:
            try:
                if timeout is None:
                    return await coro
                else:
                    return await asyncio.wait_for(coro, timeout)
            except asyncio.TimeoutError as e:
                return e
            except Exception as e:
                return e

    tasks = [_task(coro) for coro in coros]
    res = await asyncio.gather(*tasks, return_exceptions=True)
    for r in res:
        if isinstance(r, Exception):
            raise r
    return res

# %%
async def test_task():
    await asyncio.sleep(0.1)


coros = [test_task() for _ in range(10)]
res = await async_throttler(coros, max_concurrency=2)

# %%
#|hide
show_doc(this_module.is_in_event_loop)

# %%
#|export
def is_in_event_loop():
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False

# %%
#|hide
show_doc(this_module.enable_soft_interruption)

# %%
#|export
import sys

_interrupted = False
_interrupt_count = 0


class SoftInterruption(Exception):
    pass


def _soft_interruption_handler(signum, frame):
    global _interrupted, _interrupt_count
    _interrupt_count += 1
    sig_name = signal.Signals(signum).name

    if _interrupt_count < const.SOFT_INTERRUPT_COUNT:
        print(
            f"\nWARNING: {sig_name} received ({_interrupt_count}/3) — "
            f"will stop after the current operation."
        )
        _interrupted = True
    else:
        print(f"\n{sig_name} received 3 times — exiting immediately.")
        sys.exit(1)  # or: raise KeyboardInterrupt


def enable_soft_interruption():
    signal.signal(signal.SIGINT, _soft_interruption_handler)  # Ctrl-C
    signal.signal(signal.SIGTERM, _soft_interruption_handler)  # shutdown
    signal.signal(signal.SIGHUP, _soft_interruption_handler)  # logout / terminal closed


def check_interrupted():
    global _interrupted
    return _interrupted

# %%
p = Path("/Users/lukastk/dev/20251109_000000_7GfJI__boxyard")

import os

files = []
for path, dirs, filenames in os.walk(p):
    for name in filenames:
        files.append(os.path.join(path, name))

print(len(files))

# %%
#|hide
show_doc(this_module.count_files_in_dir)

# %%
#|export
def count_files_in_dir(path: Path) -> int:
    import os
    num_files = 0
    for path, dirs, filenames in os.walk(path):
        num_files += len(filenames)
    return num_files

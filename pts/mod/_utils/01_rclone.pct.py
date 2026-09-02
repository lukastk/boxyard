# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _utils.rclone

# %%
#|default_exp _utils.rclone

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();
import boxyard._utils.rclone as this_module

# %%
#|export
import os
import shlex
import json
import shutil
from enum import Enum
from boxyard import const
from pathlib import Path

from boxyard._utils import run_cmd_async

# %% [markdown]
# ## Resolving the rclone binary
#
# boxyard shells out to `rclone`. In minimal / non-interactive contexts (`ssh host
# 'boxyard ...'`, cron, agents) the caller's PATH often omits the dir rclone lives in,
# so a bare `"rclone"` dies with an opaque `FileNotFoundError`. We resolve the binary
# once, robustly, and independently of the caller's PATH.

# %%
#|exporti
# Known locations to search for the rclone binary when it is not on PATH. Covers
# Homebrew (Apple Silicon + Intel), the macOS/Linux system dirs, and snap installs.
_RCLONE_FALLBACK_DIRS = [
    "/opt/homebrew/bin",  # Homebrew on Apple Silicon
    "/usr/local/bin",  # Homebrew on Intel macs / common manual installs
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
    "/snap/bin",  # snap-installed rclone on Linux
]

# Cache of the resolved rclone binary path (resolved once per process).
_rclone_binary: str | None = None


def _read_rclone_path_from_config() -> str | None:
    """Read the optional ``rclone_path`` key from the boxyard config.toml, if present.

    Reads the raw TOML directly (not via ``get_config``) so that resolving the rclone
    binary never depends on the rest of the config being valid.
    """
    import tomllib

    config_path = Path(
        os.environ.get(const.ENV_VAR_BOXYARD_CONFIG_PATH, const.DEFAULT_CONFIG_PATH)
    ).expanduser()
    if not config_path.is_file():
        return None
    with open(config_path, "rb") as f:
        rclone_path = tomllib.load(f).get("rclone_path")
    return str(Path(rclone_path).expanduser()) if rclone_path else None


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _resolve_rclone_binary() -> str:
    """Resolve the rclone binary path, independent of the caller's PATH.

    Resolution order:
      1. ``BOXYARD_RCLONE`` env var (explicit full path)
      2. ``rclone_path`` key in the boxyard config.toml, if present
      3. ``shutil.which("rclone")`` (the caller's PATH)
      4. Known fallback install dirs (Homebrew, system dirs, snap)

    Raises a loud, actionable ``RuntimeError`` naming every location searched if rclone
    cannot be found — never a bare ``FileNotFoundError`` at exec time.
    """
    searched: list[str] = []

    # 1. Explicit env var override.
    env_path = os.environ.get(const.ENV_VAR_BOXYARD_RCLONE)
    if env_path:
        if _is_executable(Path(env_path).expanduser()):
            return str(Path(env_path).expanduser())
        raise RuntimeError(
            f"{const.ENV_VAR_BOXYARD_RCLONE} is set to '{env_path}', but no executable "
            f"rclone binary exists there. Fix the path or unset {const.ENV_VAR_BOXYARD_RCLONE}."
        )
    searched.append(f"${const.ENV_VAR_BOXYARD_RCLONE} (env var, unset)")

    # 2. `rclone_path` key in config.toml.
    config_path = _read_rclone_path_from_config()
    if config_path:
        if _is_executable(Path(config_path)):
            return config_path
        raise RuntimeError(
            f"`rclone_path` in the boxyard config.toml points to '{config_path}', but "
            f"no executable rclone binary exists there. Fix or remove the `rclone_path` key."
        )
    searched.append("`rclone_path` in config.toml (unset)")

    # 3. The caller's PATH.
    which = shutil.which("rclone")
    if which:
        return which
    searched.append("PATH (via shutil.which)")

    # 4. Known fallback install dirs.
    for d in _RCLONE_FALLBACK_DIRS:
        candidate = Path(d) / "rclone"
        if _is_executable(candidate):
            return str(candidate)
        searched.append(str(candidate))

    searched_str = "\n  - ".join(searched)
    raise RuntimeError(
        "boxyard could not find the `rclone` binary. Searched:\n  - "
        + searched_str
        + f"\n\nInstall rclone (https://rclone.org/install/), or set "
        f"{const.ENV_VAR_BOXYARD_RCLONE} to its full path "
        f"(e.g. {const.ENV_VAR_BOXYARD_RCLONE}=/opt/homebrew/bin/rclone), or add a "
        f"`rclone_path` key to your boxyard config.toml."
    )


def get_rclone_binary() -> str:
    """Return the resolved rclone binary path, resolving (and caching) it on first use."""
    global _rclone_binary
    if _rclone_binary is None:
        _rclone_binary = _resolve_rclone_binary()
    return _rclone_binary

# %% [markdown]
# Set up testing environment

# %%
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = tests_working_dir / "rclone_utils_test"
# !rm -rf {test_folder_path}

# %%
def setup_test_folder(rel_path):
    import shutil
    import inspect

    full_path = test_folder_path / rel_path
    shutil.rmtree(full_path, ignore_errors=True)
    full_path.mkdir(parents=True, exist_ok=True)

    (full_path / "my_local").mkdir(parents=True, exist_ok=True)
    (full_path / "my_local" / "file1.txt").write_text("Hello, world!")
    (full_path / "my_local" / "file2.txt").write_text("Goodbye, world!")
    (full_path / "my_remote").mkdir(parents=True, exist_ok=True)

    (full_path / "rclone.conf").write_text(
        inspect.cleandoc(f"""
    [my_remote]
    type = alias
    remote = {full_path / "my_remote"}
    """)
    )

    return full_path

# %%
#|hide
show_doc(this_module._rclone_cmd_helper)

# %%
#|exporti
def _rclone_cmd_helper(
    cmd_name: str,
    rclone_config_path: str,
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
    include: list[str],
    exclude: list[str],
    filter: list[str],
    include_file: str | None,
    exclude_file: str | None,
    filters_file: str | None,
    dry_run: bool,
    progress: bool,
    use_fast_list: bool = True,
) -> list[str]:
    source_spec = f"{source}:{source_path}" if source else source_path
    dest_spec = f"{dest}:{dest_path}" if dest else dest_path
    cmd = [
        get_rclone_binary(),
        cmd_name,
        "--config",
        rclone_config_path,
        "--links",
        source_spec,
        dest_spec,
    ]
    if dry_run:
        cmd.append("--dry-run")
    if use_fast_list:
        cmd.append("--fast-list")
    for f in include:
        cmd.append("--include")
        cmd.append(f)
    if include_file is not None:
        cmd.append("--include-from")
        cmd.append(include_file)
    for f in exclude:
        cmd.append("--exclude")
        cmd.append(f)
    if exclude_file is not None:
        cmd.append("--exclude-from")
        cmd.append(exclude_file)
    for f in filter:
        cmd.append("--filter")
        cmd.append(f)
    if filters_file is not None:
        cmd.append("--filters-file")
        cmd.append(filters_file)
    if progress:
        cmd.append("--progress")
    return cmd

# %%
#|hide
show_doc(this_module._remove_ansi_escape)

# %%
#|exporti
# Source - https://stackoverflow.com/a
# Posted by Martijn Pieters, modified by community. See post 'Timeline' for change history
# Retrieved 2025-11-10, License - CC BY-SA 4.0

import re

ansi_escape = re.compile(
    r"""
    \x1B  # ESC
    (?:   # 7-bit C1 Fe (except CSI)
        [@-Z\\-_]
    |     # or [ for CSI, followed by a control sequence
        \[
        [0-?]*  # Parameter bytes
        [ -/]*  # Intermediate bytes
        [@-~]   # Final byte
    )
""",
    re.VERBOSE,
)


# rclone signals "the thing is not there" with a specific exit code: 3 for a
# missing directory and 4 for a missing file. EVERY OTHER non-zero exit is a
# real failure -- a network blip is exit 1, an auth failure exit 1, and so on.
#
# Conflating the two is dangerous, not merely imprecise. `rclone_lsjson` and
# `rclone_cat` used to return None for any non-zero exit, so a transient SFTP
# failure was reported as "the remote path does not exist". That made
# `scan_and_rebuild_remote_index_cache` persist an EMPTY index after a blip,
# and made `SyncRecord.rclone_read` report "no remote sync record" when it had
# merely failed to read one -- which the sync state machine reads as a
# different world.
RCLONE_EXIT_DIR_NOT_FOUND = 3
RCLONE_EXIT_FILE_NOT_FOUND = 4
RCLONE_ABSENT_EXIT_CODES = (RCLONE_EXIT_DIR_NOT_FOUND, RCLONE_EXIT_FILE_NOT_FOUND)


class RcloneFailed(Exception):
    """An rclone command failed for a reason other than the path being absent."""

    def __init__(self, cmd: list[str], ret_code: int, stdout: str, stderr: str):
        self.cmd = cmd
        self.ret_code = ret_code
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"`rclone {cmd[1] if len(cmd) > 1 else '?'}` failed with exit code "
            f"{ret_code} (this is NOT 'path not found'; the remote may be "
            f"unreachable).\n{stderr.strip()}"
        )


def _remove_ansi_escape(text: str) -> str:
    return ansi_escape.sub("", text)

# %%
_remove_ansi_escape("Hello \x1b[31mWorld\x1b[0m")

# %%
#|hide
show_doc(this_module.rclone_copy)

# %%
#|export
async def rclone_copy(
    rclone_config_path: str,
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
    include: list[str] = [],
    exclude: list[str] = [],
    filter: list[str] = [],
    include_file: str | None = None,
    exclude_file: str | None = None,
    filters_file: str | None = None,
    dry_run: bool = False,
    progress: bool = False,
    return_command: bool = False,
    verbose=False,
) -> bool:
    cmd = _rclone_cmd_helper(
        "copy",
        rclone_config_path,
        source,
        source_path,
        dest,
        dest_path,
        include,
        exclude,
        filter,
        include_file,
        exclude_file,
        filters_file,
        dry_run,
        progress,
    )
    if not return_command:
        ret_code, stdout, stderr = await run_cmd_async(cmd)
        if verbose:
            print(stdout)
            print(stderr)
        return ret_code == 0, stdout, stderr
    else:
        return shlex.join(cmd)

# %%
_path = setup_test_folder("copy")

res = await rclone_copy(
    _path / "rclone.conf",
    source="",
    source_path=_path / "my_local",
    dest="my_remote",
    dest_path="",
    include=[],
    exclude=[],
    filter=[],
    include_file=None,
    exclude_file=None,
    filters_file=None,
    dry_run=False,
    verbose=True,
)

assert res
ls = [f.name for f in (_path / "my_remote").iterdir()]
assert "file1.txt" in ls
assert "file2.txt" in ls

# %%
#|hide
show_doc(this_module.rclone_copyto)

# %%
#|export
async def rclone_copyto(
    rclone_config_path: str,
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
    dry_run: bool = False,
    progress: bool = False,
    return_command: bool = False,
    verbose=False,
) -> bool:
    source_spec = f"{source}:{source_path}" if source else source_path
    dest_spec = f"{dest}:{dest_path}" if dest else dest_path
    cmd = [get_rclone_binary(), "copyto", "--config", rclone_config_path, source_spec, dest_spec]
    # `dry_run` was accepted and then never emitted, so a caller asking for a
    # dry run silently WROTE. No current call site passes True, which is why
    # nothing has broken, but the parameter was a live trap.
    if dry_run:
        cmd.append("--dry-run")
    if progress:
        cmd.append("--progress")
    if not return_command:
        ret_code, stdout, stderr = await run_cmd_async(cmd)
        if verbose:
            print(stdout)
            print(stderr)
        return ret_code == 0, stdout, stderr
    else:
        return shlex.join(cmd)

# %%
_path = setup_test_folder("copyto")

res = await rclone_copyto(
    _path / "rclone.conf",
    source="",
    source_path=_path / "my_local" / "file1.txt",
    dest="my_remote",
    dest_path="file1_copied.txt",
    dry_run=False,
    verbose=True,
)

assert res
ls = [f.name for f in (_path / "my_remote").iterdir()]
assert "file1_copied.txt" in ls

# %%
#|hide
show_doc(this_module.rclone_sync)

# %% [markdown]
# ## `rclone_check` — "would a sync actually move anything?"
#
# Used by the write-ownership probe. `rclone check --combined -` emits one line
# per path with a documented single-character prefix (`=` identical, `+` only
# on the source, `-` only on the destination, `*` differing, `!` error), which
# is a stable machine-readable answer — unlike the text of `sync --dry-run`.
#
# That distinction is load-bearing rather than cosmetic. Measured: with
# identical content but a different mtime, `sync --dry-run` prints
# `NOTICE: f.txt: Skipped update modification time as --dry-run is set`, so any
# "did the dry run mention this file?" test calls an unchanged box changed —
# which is precisely the false positive the probe exists to eliminate.
# `check --combined` reports `= f.txt` for the same pair.
#
# One honest limitation: `check` compares by hash where both sides offer one
# and falls back to size otherwise, so on a backend with no hash support two
# same-size files with different content compare equal. That failure is in the
# safe direction here — see the probe's caller.

# %%
#|export
async def rclone_check(
    rclone_config_path: str,
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
    include: list[str] = [],
    exclude: list[str] = [],
    filter: list[str] = [],
    include_file: str | None = None,
    exclude_file: str | None = None,
    filters_file: str | None = None,
) -> tuple[bool, list[str]]:
    """
    Compare `source` against `dest` under the given filters.

    Returns `(answered, differing_paths)`. `answered` is False when the check
    could not be performed at all (an unreachable remote, a bad config) — the
    caller must not read that as "no differences", because rclone exits
    non-zero both for "found differences" and for "could not look".
    """
    cmd = _rclone_cmd_helper(
        "check",
        rclone_config_path,
        source,
        source_path,
        dest,
        dest_path,
        include,
        exclude,
        filter,
        include_file,
        exclude_file,
        filters_file,
        False,  # dry_run is meaningless for a read-only comparison
        False,  # progress
    )
    cmd += ["--combined", "-"]
    ret_code, stdout, stderr = await run_cmd_async(cmd)

    lines = [line for line in stdout.splitlines() if line.strip()]
    differing = [line[2:] for line in lines if not line.startswith("= ")]

    # Exit 0 means the comparison ran and found everything identical -- including
    # the both-sides-empty case, which produces no lines at all. A non-zero exit
    # with no lines means the comparison did not happen (rclone reports "found
    # differences" and "could not look" with the same code), so we cannot answer.
    if ret_code != 0 and not lines:
        return False, []
    return True, differing

# %%
#|export
async def rclone_sync(
    rclone_config_path: str,
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
    include: list[str] = [],
    exclude: list[str] = [],
    filter: list[str] = [],
    include_file: str | None = None,
    exclude_file: str | None = None,
    filters_file: str | None = None,
    backup_path: str | None = None,
    dry_run: bool = False,
    progress: bool = False,
    return_command: bool = False,
    verbose=False,
) -> bool:
    cmd = _rclone_cmd_helper(
        "sync",
        rclone_config_path,
        source,
        source_path,
        dest,
        dest_path,
        include,
        exclude,
        filter,
        include_file,
        exclude_file,
        filters_file,
        dry_run,
        progress,
    )
    if backup_path:
        cmd.append("--backup-dir")
        cmd.append(backup_path)
    if not return_command:
        ret_code, stdout, stderr = await run_cmd_async(cmd)
        if verbose:
            print(stdout)
            print(stderr)
        return ret_code == 0, stdout, stderr
    else:
        return shlex.join(cmd)

# %%
_path = setup_test_folder("sync")

res = await rclone_sync(
    _path / "rclone.conf",
    source="",
    source_path=_path / "my_local",
    dest="my_remote",
    dest_path="",
    include=[],
    exclude=[],
    filter=[],
    include_file=None,
    exclude_file=None,
    filters_file=None,
    dry_run=False,
    verbose=True,
)

assert res
ls = [f.name for f in (_path / "my_remote").iterdir()]
assert "file1.txt" in ls
assert "file2.txt" in ls

# %%
#|hide
show_doc(this_module.rclone_bisync)

# %%
#|export
class BisyncResult(Enum):
    SUCCESS = "success"
    CONFLICTS = "conflicts"
    ERROR_NEEDS_RESYNC = "needs_resync"
    ERROR_ALL_FILES_CHANGED = "all_files_changed"
    ERROR_OTHER = "other_error"


async def rclone_bisync(
    rclone_config_path: str,
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
    resync: bool,
    force: bool,
    include: list[str] = [],
    exclude: list[str] = [],
    filter: list[str] = [],
    include_file: str | None = None,
    exclude_file: str | None = None,
    filters_file: str | None = None,
    dry_run: bool = False,
    progress: bool = False,
    return_command: bool = False,
    verbose: bool = False,
) -> BisyncResult:
    cmd = _rclone_cmd_helper(
        "bisync",
        rclone_config_path,
        source,
        source_path,
        dest,
        dest_path,
        include,
        exclude,
        filter,
        include_file,
        exclude_file,
        filters_file,
        dry_run,
        progress,
    )
    if resync:
        cmd.append("--resync")
    if force:
        cmd.append("--force")
    if not return_command:
        ret_code, stdout, stderr = await run_cmd_async(cmd)
        if verbose:
            print(stdout)
            print(stderr)
        stdout_clean = _remove_ansi_escape(stdout)
        stderr_clean = _remove_ansi_escape(stderr)
        if "ERROR : Bisync aborted. Must run --resync to recover." in stderr_clean:
            return BisyncResult.ERROR_NEEDS_RESYNC, stdout, stderr
        if "ERROR : Safety abort: all files were changed" in stderr_clean:
            return BisyncResult.ERROR_ALL_FILES_CHANGED, stdout, stderr
        if ret_code != 0:
            return BisyncResult.ERROR_OTHER, stdout, stderr
        if "NOTICE: - WARNING  New or changed in both paths" in stderr_clean:
            return BisyncResult.CONFLICTS, stdout, stderr
        return BisyncResult.SUCCESS, stdout, stderr
    else:
        return shlex.join([c.as_posix() if type(c) == Path else str(c) for c in cmd])

# %%
#|hide
show_doc(this_module.rclone_mkdir)

# %%
#|export
async def rclone_mkdir(
    rclone_config_path: str,
    source: str,
    source_path: str,
    timeout: float | None = const.RCLONE_LISTING_TIMEOUT,
) -> dict | None:
    """
    Create a directory in rclone. Will not fail if the directory already exists. If parent directories are missing, they will be created.
    """
    source_str = f"{source}:{source_path}" if source else source_path
    cmd = [get_rclone_binary(), "mkdir", "--config", rclone_config_path, source_str]
    ret_code, stdout, stderr = await run_cmd_async(cmd, timeout=timeout)
    if ret_code != 0:
        raise Exception(stderr)

# %%
#|hide
show_doc(this_module.rclone_lsjson)

# %%
#|export
async def rclone_lsjson(
    rclone_config_path: str,
    source: str,
    source_path: str,
    dirs_only: bool = False,
    files_only: bool = False,
    recursive: bool = False,
    max_depth: int | None = None,
    symlinks: bool = True,
    filter: list[str] = [],
    timeout: float | None = const.RCLONE_LISTING_TIMEOUT,
) -> dict | None:
    source_str = f"{source}:{source_path}" if source else source_path
    cmd = [get_rclone_binary(), "lsjson", "--config", rclone_config_path, source_str]
    if dirs_only:
        cmd.append("--dirs-only")
    if files_only:
        cmd.append("--files-only")
    if recursive:
        cmd.append("--recursive")
    if symlinks:
        cmd.append("--links")
    if max_depth is not None:
        cmd.append("--max-depth")
        cmd.append(str(max_depth))
    cmd.append("--fast-list")

    for f in filter:
        cmd.append("--filter")
        cmd.append(f)
    ret_code, stdout, stderr = await run_cmd_async(cmd, timeout=timeout)
    if ret_code in RCLONE_ABSENT_EXIT_CODES:
        return None  # Genuinely not there.
    if ret_code != 0:
        raise RcloneFailed(cmd, ret_code, stdout, stderr)
    return json.loads(stdout)

# %%
#|hide
show_doc(this_module.rclone_path_exists)

# %%
#|export
async def rclone_path_exists(
    rclone_config_path: str,
    source: str,
    source_path: str,
) -> tuple[bool, bool]:
    """
    Check if a path exists in rclone.
    Returns a tuple of (exists, is_dir).
    """
    if Path(source_path).as_posix() == ".":  # Special case for the root directory
        return (True, True)

    parent_path = Path(source_path).parent if len(Path(source_path).parts) > 1 else ""
    ls = await rclone_lsjson(
        rclone_config_path,
        source,
        parent_path,
    )
    if ls is None:
        return (False, False)
    ls = {f["Name"]: f for f in ls}
    exists = Path(source_path).name in ls
    is_dir = ls[Path(source_path).name]["IsDir"] if exists else False
    return (exists, is_dir)

# %%
assert await rclone_path_exists(
    _path / "rclone.conf",
    source="",
    source_path=_path / "my_remote",
) == (True, True)

# %%
#|hide
show_doc(this_module.rclone_purge)

# %%
#|export
async def rclone_purge(
    rclone_config_path: str,
    source: str,
    source_path: str,
) -> None:
    """
    Delete a directory and everything inside it.

    Raises `RcloneFailed` if the purge does not succeed -- INCLUDING when the
    directory was never there. Callers for whom absence is a legitimate outcome
    want `rclone_purge_absent_ok` instead.

    It deliberately returns nothing to check. It used to return `ret_code == 0`,
    and the single most important caller did not look: `sync_helper` purges the
    backup directory after a successful sync and dropped the result on the
    floor, so every failed purge leaked in silence. By 2026-08 the remote held
    1,186 orphaned backup directories, 116.4 GiB, reaching back to 2025-11 --
    all of them invisible. An exception cannot be dropped by accident, and a
    caller that really does want to carry on now has to write down that it is
    doing so. See the `orphaned-sync-backups` check in `boxyard doctor`.
    """
    source_str = f"{source}:{source_path}" if source else source_path
    # `--links` for the same reason every sync/copy this module builds passes
    # it: boxyard PUTS symlinks on the remote, so purge has to be able to see
    # them to remove them. Without it, purging a directory that contains a
    # symlink fails with "directory not empty" (measured, rclone v1.75.0), and
    # the caller reads that as a genuine failure.
    #
    # It bites a remote whose backend stores symlinks AS symlinks -- a local
    # path or an alias to one. On sftp, which is what the real yard uses,
    # `--links` stores them as ordinary `.rclonelink` files that purge already
    # removed, so this fixes the test harness and closes an asymmetry rather
    # than explaining any leak seen in the field.
    cmd = [
        get_rclone_binary(), "purge", "--config", rclone_config_path,
        "--links", source_str,
    ]
    ret_code, stdout, stderr = await run_cmd_async(cmd)
    if ret_code != 0:
        raise RcloneFailed(cmd, ret_code, stdout, stderr)


async def rclone_purge_absent_ok(
    rclone_config_path: str,
    source: str,
    source_path: str,
) -> bool:
    """
    Purge a directory that may legitimately not be there.

    Returns True if a directory was purged, False if there was nothing to
    purge. Every other failure raises `RcloneFailed`.

    `rclone purge` does NOT follow the exit-code convention that
    `RCLONE_ABSENT_EXIT_CODES` encodes: purging a missing directory exits 1 --
    the very same code as an unreachable remote (measured against rclone
    v1.75.0 on 2026-09-01). Absence therefore cannot be read off the exit code
    at all, so this asks the remote directly rather than pattern-matching
    rclone's error text, which would silently start conflating the two the day
    rclone rewords it. The probe runs only after a purge has already failed, so
    the ordinary path pays nothing for it.
    """
    try:
        await rclone_purge(rclone_config_path, source, source_path)
    except RcloneFailed:
        exists, _is_dir = await rclone_path_exists(
            rclone_config_path, source, source_path
        )
        if exists:
            raise
        return False
    return True

# %%
_path = setup_test_folder("purge")

await rclone_purge(
    _path / "rclone.conf",
    source="my_remote",
    source_path="",
)

# %%
# Purging what is not there is not an error for `rclone_purge_absent_ok`...
assert not await rclone_purge_absent_ok(
    _path / "rclone.conf",
    source="my_remote",
    source_path="never-existed",
)

# %%
# ...but it IS one for `rclone_purge`, which never guesses.
try:
    await rclone_purge(
        _path / "rclone.conf",
        source="my_remote",
        source_path="never-existed",
    )
    raise AssertionError("rclone_purge swallowed a failure")
except RcloneFailed:
    pass

# %%
#|hide
show_doc(this_module.rclone_cat)

# %%
#|export
async def rclone_cat(
    rclone_config_path: str,
    source: str,
    source_path: str,
    timeout: float | None = const.RCLONE_LISTING_TIMEOUT,
) -> tuple[bool, str | None]:
    source_str = f"{source}:{source_path}" if source else source_path
    cmd = [get_rclone_binary(), "cat", "--config", rclone_config_path, source_str]
    ret_code, stdout, stderr = await run_cmd_async(cmd, timeout=timeout)
    if ret_code == 0:
        return True, stdout
    if ret_code in RCLONE_ABSENT_EXIT_CODES:
        return False, None  # Genuinely not there.
    raise RcloneFailed(cmd, ret_code, stdout, stderr)

# %%
_path = setup_test_folder("cat")

res = await rclone_sync(
    _path / "rclone.conf",
    source="",
    source_path=_path / "my_local",
    dest="my_remote",
    dest_path="",
    include=[],
    exclude=[],
    filter=[],
    include_file=None,
    exclude_file=None,
    filters_file=None,
    dry_run=False,
    verbose=True,
)

res, content = await rclone_cat(
    _path / "rclone.conf",
    source="my_remote",
    source_path="file1.txt",
)

assert res
assert content == "Hello, world!"

# %%
#|hide
show_doc(this_module.rclone_move)

# %%
#|export
async def rclone_move(
    rclone_config_path: str,
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
) -> tuple[bool, str | None]:
    source_str = f"{source}:{source_path}" if source else source_path
    dest_str = f"{dest}:{dest_path}" if dest else dest_path
    cmd = [get_rclone_binary(), "move", "--config", rclone_config_path, source_str, dest_str]
    ret_code, stdout, stderr = await run_cmd_async(cmd)
    if ret_code == 0:
        return True, stdout
    else:
        return False, stderr

# %%
_path = setup_test_folder("move")
(_path / "my_remote" / "folder1").mkdir(parents=True, exist_ok=True)

res, _ = await rclone_move(
    _path / "rclone.conf",
    source="my_remote",
    source_path="folder1",
    dest="my_remote",
    dest_path="folder2",
)
assert res

assert not (_path / "my_remote" / "folder1").exists()
assert (_path / "my_remote" / "folder2").exists()

# %%
#|hide
show_doc(this_module.rclone_moveto)

# %%
#|export
async def rclone_moveto(
    rclone_config_path: str,
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
) -> tuple[bool, str | None]:
    """
    Move/rename a single file or directory.
    Unlike rclone_move, this renames the source to the exact dest path.
    """
    source_str = f"{source}:{source_path}" if source else source_path
    dest_str = f"{dest}:{dest_path}" if dest else dest_path
    cmd = [get_rclone_binary(), "moveto", "--config", rclone_config_path, source_str, dest_str]
    ret_code, stdout, stderr = await run_cmd_async(cmd)
    if ret_code == 0:
        return True, stdout
    else:
        return False, stderr

# %%
_path = setup_test_folder("moveto")
(_path / "my_remote" / "old_name").mkdir(parents=True, exist_ok=True)
(_path / "my_remote" / "old_name" / "file.txt").write_text("test")

res, _ = await rclone_moveto(
    _path / "rclone.conf",
    source="my_remote",
    source_path="old_name",
    dest="my_remote",
    dest_path="new_name",
)
assert res

assert not (_path / "my_remote" / "old_name").exists()
assert (_path / "my_remote" / "new_name").exists()
assert (_path / "my_remote" / "new_name" / "file.txt").read_text() == "test"

# %%
#|hide
show_doc(this_module.rclone_write)

# %%
#|export
async def rclone_write(
    rclone_config_path: str,
    dest: str,
    dest_path: str,
    content: str,
) -> bool:
    """
    Write content to a remote file.
    Creates parent directories if they don't exist.
    """
    import tempfile

    # Write to temp file first, then copy
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.tmp') as f:
        f.write(content)
        temp_path = f.name

    try:
        success, _, _ = await rclone_copyto(
            rclone_config_path=rclone_config_path,
            source="",
            source_path=temp_path,
            dest=dest,
            dest_path=dest_path,
        )
        return success
    finally:
        Path(temp_path).unlink(missing_ok=True)

# %%
_path = setup_test_folder("write")

res = await rclone_write(
    _path / "rclone.conf",
    dest="my_remote",
    dest_path="written_file.txt",
    content="Hello from rclone_write!",
)
assert res

content = (_path / "my_remote" / "written_file.txt").read_text()
assert content == "Hello from rclone_write!"

# %%
#|hide
show_doc(this_module.rclone_delete)

# %%
#|export
async def rclone_delete(
    rclone_config_path: str,
    dest: str,
    dest_path: str,
) -> None:
    """
    Delete a single remote file.

    Raises `RcloneFailed` if the delete does not succeed. Like `rclone_purge`,
    it returns nothing to check: it used to return `ret_code == 0` and its only
    caller -- `remove_tombstone`, which promises the tombstone is gone -- did
    not look, so a failed delete left the box tombstoned while every caller
    believed it had been revived.
    """
    dest_str = f"{dest}:{dest_path}" if dest else dest_path
    cmd = [get_rclone_binary(), "deletefile", "--config", rclone_config_path, dest_str]
    ret_code, stdout, stderr = await run_cmd_async(cmd)
    if ret_code != 0:
        raise RcloneFailed(cmd, ret_code, stdout, stderr)


async def rclone_delete_absent_ok(
    rclone_config_path: str,
    dest: str,
    dest_path: str,
) -> bool:
    """
    Delete a single remote file that may legitimately not be there.

    Returns True if a file was deleted, False if there was nothing to delete.
    Every other failure raises `RcloneFailed`.

    Unlike `rclone purge`, `rclone deletefile` DOES follow the exit-code
    convention `RCLONE_ABSENT_EXIT_CODES` encodes, so absence can be read
    straight off the code and no probe is needed. Measured against rclone
    v1.75.0 on 2026-09-01:

        file exists        -> 0
        file absent        -> 4   (RCLONE_EXIT_FILE_NOT_FOUND)
        remote unreachable -> 1

    That is why this does not mirror `rclone_purge_absent_ok`'s extra
    `rclone_path_exists` probe: purge exits 1 for BOTH a missing directory and
    an unreachable remote, which is what forces it to ask. Do not "harmonise"
    the two -- the difference is measured, not accidental.
    """
    dest_str = f"{dest}:{dest_path}" if dest else dest_path
    cmd = [get_rclone_binary(), "deletefile", "--config", rclone_config_path, dest_str]
    ret_code, stdout, stderr = await run_cmd_async(cmd)
    if ret_code in RCLONE_ABSENT_EXIT_CODES:
        return False
    if ret_code != 0:
        raise RcloneFailed(cmd, ret_code, stdout, stderr)
    return True

# %%
_path = setup_test_folder("delete")
(_path / "my_remote" / "to_delete.txt").write_text("delete me")

assert (_path / "my_remote" / "to_delete.txt").exists()

await rclone_delete(
    _path / "rclone.conf",
    dest="my_remote",
    dest_path="to_delete.txt",
)
assert not (_path / "my_remote" / "to_delete.txt").exists()

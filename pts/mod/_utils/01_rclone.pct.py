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
import shlex
import json
from enum import Enum
from boxyard import const
from pathlib import Path

from boxyard._utils import run_cmd_async

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
        "rclone",
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
    cmd = ["rclone", "copyto", "--config", rclone_config_path, source_spec, dest_spec]
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
) -> dict | None:
    """
    Create a directory in rclone. Will not fail if the directory already exists. If parent directories are missing, they will be created.
    """
    source_str = f"{source}:{source_path}" if source else source_path
    cmd = ["rclone", "mkdir", "--config", rclone_config_path, source_str]
    ret_code, stdout, stderr = await run_cmd_async(cmd)
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
) -> dict | None:
    source_str = f"{source}:{source_path}" if source else source_path
    cmd = ["rclone", "lsjson", "--config", rclone_config_path, source_str]
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
    ret_code, stdout, stderr = await run_cmd_async(cmd)
    if ret_code != 0:
        return None
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
) -> bool:
    source_str = f"{source}:{source_path}" if source else source_path
    cmd = ["rclone", "purge", "--config", rclone_config_path, source_str]
    ret_code, stdout, stderr = await run_cmd_async(cmd)
    return ret_code == 0

# %%
_path = setup_test_folder("purge")

assert await rclone_purge(
    _path / "rclone.conf",
    source="my_remote",
    source_path="",
)

# %%
#|hide
show_doc(this_module.rclone_cat)

# %%
#|export
async def rclone_cat(
    rclone_config_path: str,
    source: str,
    source_path: str,
) -> tuple[bool, str | None]:
    source_str = f"{source}:{source_path}" if source else source_path
    cmd = ["rclone", "cat", "--config", rclone_config_path, source_str]
    ret_code, stdout, stderr = await run_cmd_async(cmd)
    if ret_code == 0:
        return True, stdout
    else:
        return False, None

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
    cmd = ["rclone", "move", "--config", rclone_config_path, source_str, dest_str]
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
    cmd = ["rclone", "moveto", "--config", rclone_config_path, source_str, dest_str]
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
) -> bool:
    """
    Delete a single remote file.
    """
    dest_str = f"{dest}:{dest_path}" if dest else dest_path
    cmd = ["rclone", "deletefile", "--config", rclone_config_path, dest_str]
    ret_code, stdout, stderr = await run_cmd_async(cmd)
    return ret_code == 0

# %%
_path = setup_test_folder("delete")
(_path / "my_remote" / "to_delete.txt").write_text("delete me")

assert (_path / "my_remote" / "to_delete.txt").exists()

res = await rclone_delete(
    _path / "rclone.conf",
    dest="my_remote",
    dest_path="to_delete.txt",
)
assert res
assert not (_path / "my_remote" / "to_delete.txt").exists()

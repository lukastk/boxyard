# %% [markdown]
# # _utils.rclone

# %%
#|default_exp _utils.rclone

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import repoyard._utils.rclone as this_module

# %%
#|export
import subprocess
import shlex
import json
from enum import Enum
from repoyard import const
import repoyard.config
from pathlib import Path

# %% [markdown]
# Set up testing environment

# %%
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = tests_working_dir / "rclone_utils_test"
# !rm -rf {test_folder_path}

# %%
def setup_test_folder(rel_path):
    import shutil, inspect, os
    full_path = test_folder_path / rel_path
    shutil.rmtree(full_path, ignore_errors=True)
    full_path.mkdir(parents=True, exist_ok=True)
    
    (full_path / "my_local").mkdir(parents=True, exist_ok=True)
    (full_path / "my_local" / "file1.txt").write_text("Hello, world!")
    (full_path / "my_local" / "file2.txt").write_text("Goodbye, world!")
    (full_path / "my_remote").mkdir(parents=True, exist_ok=True)
    
    (full_path / "rclone.conf").write_text(inspect.cleandoc(f"""
    [my_remote]
    type = alias
    remote = {full_path / "my_remote"}
    """))
    
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
    include_file: str|None,
    exclude_file: str|None,
    filters_file: str|None,
    dry_run: bool,
) -> list[str]:
    source_spec = f"{source}:{source_path}" if source else source_path
    dest_spec = f"{dest}:{dest_path}" if dest else dest_path
    cmd = ["rclone", cmd_name, '--config', rclone_config_path, source_spec, dest_spec]
    if dry_run:
        cmd.append("--dry-run")
    for f in include:
        cmd.append(f"--include")
        cmd.append(f)
    if include_file is not None:
        cmd.append(f"--include-from")
        cmd.append(include_file)
    for f in exclude:
        cmd.append(f"--exclude")
        cmd.append(f)
    if exclude_file is not None:
        cmd.append(f"--exclude-from")
        cmd.append(exclude_file)
    for f in filter:
        cmd.append(f"--filter")
        cmd.append(f)
    if filters_file is not None:
        cmd.append("--filters-file")
        cmd.append(filters_file)
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
ansi_escape = re.compile(r'''
    \x1B  # ESC
    (?:   # 7-bit C1 Fe (except CSI)
        [@-Z\\-_]
    |     # or [ for CSI, followed by a control sequence
        \[
        [0-?]*  # Parameter bytes
        [ -/]*  # Intermediate bytes
        [@-~]   # Final byte
    )
''', re.VERBOSE)

def _remove_ansi_escape(text: str) -> str:
    return ansi_escape.sub('', text)


# %%
_remove_ansi_escape("Hello \x1B[31mWorld\x1B[0m")

# %%
#|hide
show_doc(this_module.rclone_copy)


# %%
#|export
def rclone_copy(
    rclone_config_path: str,
    source: str,
    source_path: str,   
    dest: str,
    dest_path: str,
    include: list[str],
    exclude: list[str],
    filter: list[str],
    include_file: str|None,
    exclude_file: str|None,
    filters_file: str|None,
    dry_run: bool,
    return_command: bool=False,
    verbose=True,
) -> bool:
    cmd = _rclone_cmd_helper("copy", rclone_config_path, source, source_path, dest, dest_path, include, exclude, filter, include_file, exclude_file, filters_file, dry_run)
    if not return_command:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if verbose:
            print(result.stdout)
            print(result.stderr)
        return result.returncode == 0, result.stdout, result.stderr
    else:
        return shlex.join(cmd)


# %%
_path = setup_test_folder('copy')

res = rclone_copy(
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
show_doc(this_module.rclone_sync)


# %%
#|export
def rclone_sync(
    rclone_config_path: str,
    source: str,
    source_path: str,   
    dest: str,
    dest_path: str,
    include: list[str],
    exclude: list[str],
    filter: list[str],
    include_file: str|None,
    exclude_file: str|None,
    filters_file: str|None,
    dry_run: bool,
    return_command: bool=False,
    verbose=True,
) -> bool:
    cmd = _rclone_cmd_helper("sync", rclone_config_path, source, source_path, dest, dest_path, include, exclude, filter, include_file, exclude_file, filters_file, dry_run)
    if not return_command:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if verbose:
            print(result.stdout)
            print(result.stderr)
        return result.returncode == 0, result.stdout, result.stderr
    else:
        return shlex.join(cmd)


# %%
_path = setup_test_folder('sync')

res = rclone_sync(
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

def rclone_bisync(
    rclone_config_path: str,
    source: str,
    source_path: str,   
    dest: str,
    dest_path: str,
    include: list[str],
    exclude: list[str],
    filter: list[str],
    include_file: str|None,
    exclude_file: str|None,
    filters_file: str|None,
    dry_run: bool,
    resync: bool,
    force: bool,
    return_command: bool=False,
    verbose: bool=False,
) -> BisyncResult:
    cmd = _rclone_cmd_helper("bisync", rclone_config_path, source, source_path, dest, dest_path, include, exclude, filter, include_file, exclude_file, filters_file, dry_run)
    if resync: cmd.append("--resync")
    if force: cmd.append("--force")
    if not return_command:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if verbose:
            print(result.stdout)
            print(result.stderr)
        stdout_clean = _remove_ansi_escape(result.stdout)
        stderr_clean = _remove_ansi_escape(result.stderr)
        if "ERROR : Bisync aborted. Must run --resync to recover." in stderr_clean:
            return BisyncResult.ERROR_NEEDS_RESYNC, result.stdout, result.stderr
        if "ERROR : Safety abort: all files were changed" in stderr_clean:
            return BisyncResult.ERROR_ALL_FILES_CHANGED, result.stdout, result.stderr
        if result.returncode != 0:
            return BisyncResult.ERROR_OTHER, result.stdout, result.stderr
        if "NOTICE: - WARNING  New or changed in both paths" in stderr_clean:
            return BisyncResult.CONFLICTS, result.stdout, result.stderr
        return BisyncResult.SUCCESS, result.stdout, result.stderr
    else:
        return shlex.join([c.as_posix() if type(c) == Path else str(c) for c in cmd])


# %%
_path = setup_test_folder('bisync')

res, stdout, stderr = rclone_bisync(
    _path / "rclone.conf",
    source="",
    source_path=_path / "my_local",
    dest="my_remote",
    dest_path="",
    include=[],
    include_file=None,
    exclude=[],
    exclude_file=None,
    filter=[],
    filters_file=None,
    dry_run=False,
    resync=False,
    force=False,
    verbose=True,
)

assert res == BisyncResult.ERROR_NEEDS_RESYNC

# %%
_path = setup_test_folder('bisync')

res, stdout, stderr = rclone_bisync(
    _path / "rclone.conf",
    source="",
    source_path=_path / "my_local",
    dest="my_remote",
    dest_path="",
    include=[],
    exclude=[],
    include_file=None,
    exclude_file=None,
    filter=[],
    filters_file=None,
    dry_run=False,
    resync=True,
    force=False,
    verbose=True,
)

assert res == BisyncResult.SUCCESS

# %%
# !echo "will cause conflict" > {_path / "my_local" / "file1.txt"}
# !echo "will cause conflict!" > {_path / "my_remote" / "file1.txt"}

res, stdout, stderr = rclone_bisync(
    _path / "rclone.conf",
    source="",
    source_path=_path / "my_local",
    dest="my_remote",
    dest_path="",
    include=[],
    exclude=[],
    include_file=None,
    exclude_file=None,
    filter=[],
    filters_file=None,
    dry_run=False,
    resync=False,
    force=False,
    verbose=True,
)

assert res == BisyncResult.CONFLICTS

# %%
#|hide
show_doc(this_module.rclone_mkdir)


# %%
#|export
def rclone_mkdir(
    rclone_config_path: str,
    source: str,
    source_path: str,
) -> dict|None:
    """
    Create a directory in rclone. Will not fail if the directory already exists. If parent directories are missing, they will be created.
    """
    source_str = f"{source}:{source_path}" if source else source_path
    cmd = ["rclone", "mkdir", '--config', rclone_config_path, source_str]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(result.stderr)


# %%
#|hide
show_doc(this_module.rclone_lsjson)


# %%
#|export
def rclone_lsjson(
    rclone_config_path: str,
    source: str,
    source_path: str,
) -> dict|None:
    source_str = f"{source}:{source_path}" if source else source_path
    cmd = ["rclone", "lsjson", '--config', rclone_config_path, source_str]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


# %%
_path = setup_test_folder('lsjson')

res, stdout, stderr = rclone_bisync(
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
    resync=True,
    force=False,
    verbose=True,
)

res = rclone_lsjson(
    _path / "rclone.conf",
    source="my_remote",
    source_path="",
)
file_names = [f["Name"] for f in res]
assert "file1.txt" in file_names
assert "file2.txt" in file_names

# %%
rclone_lsjson(
    _path / "rclone.conf",
    source="my_remote",
    source_path="",
)

# %%
#|hide
show_doc(this_module.rclone_path_exists)


# %%
#|export
def rclone_path_exists(
    rclone_config_path: str,
    source: str,
    source_path: str,
) -> tuple[bool, bool]:
    """
    Check if a path exists in rclone.
    Returns a tuple of (exists, is_dir).
    """
    if Path(source_path).as_posix() == ".": # Special case for the root directory
        return (True, True)
    
    parent_path = Path(source_path).parent if len(Path(source_path).parts) > 1 else ""
    ls = rclone_lsjson(
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
assert rclone_path_exists(
    _path / "rclone.conf",
    source="my_remote",
    source_path="file1.txt",
) == (True, False)

# %%
assert rclone_path_exists(
    _path / "rclone.conf",
    source="",
    source_path=_path / "my_remote",
) == (True, True)

# %%
#|hide
show_doc(this_module.rclone_purge)


# %%
#|export
def rclone_purge(
    rclone_config_path: str,
    source: str,
    source_path: str,
) -> bool:
    source_str = f"{source}:{source_path}" if source else source_path
    cmd = ["rclone", "purge", '--config', rclone_config_path, source_str]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


# %%
_path = setup_test_folder('purge')

assert rclone_purge(
    _path / "rclone.conf",
    source="my_remote",
    source_path="",
)

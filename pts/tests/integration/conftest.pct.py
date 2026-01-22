# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Integration Test Configuration and Fixtures
#
# Shared fixtures and utilities for integration tests.

# %%
#|default_exp integration.conftest

# %%
#|export
import subprocess
from pathlib import Path
import tempfile
import toml
import inspect
import pytest

from repoyard.cmds import init_repoyard
from repoyard.config import get_config


# ============================================================================
# Fixtures for creating test repoyards
# ============================================================================

# %%
#|export
@pytest.fixture
def temp_repoyard(tmp_path):
    """Create a single temporary repoyard for testing.

    Returns:
        tuple: (remote_name, remote_rclone_path, config, config_path, data_path)
    """
    remote_name = "test_remote"
    remote_rclone_path = tmp_path / "remote_storage"
    remote_rclone_path.mkdir()

    test_folder_path = tmp_path / "repoyard"
    test_folder_path.mkdir()

    config_path = test_folder_path / ".config" / "repoyard" / "config.toml"
    data_path = test_folder_path / ".repoyard"

    # Run init
    init_repoyard(config_path=config_path, data_path=data_path, verbose=False)
    config = get_config(config_path)

    # Add a storage location
    config_dump = toml.load(config_path)
    config_dump["user_repos_path"] = (test_folder_path / "user_repos").as_posix()
    config_dump["user_repo_groups_path"] = (
        test_folder_path / "user_repo_groups"
    ).as_posix()
    config_dump["storage_locations"][remote_name] = {
        "storage_type": "rclone",
        "store_path": "repoyard",
    }

    # Set up a rclone remote path (alias to local folder)
    config.rclone_config_path.write_text(
        config.rclone_config_path.read_text()
        + "\n"
        + inspect.cleandoc(f"""
    [{remote_name}]
    type = alias
    remote = {remote_rclone_path}
    """)
    )

    config_path.write_text(toml.dumps(config_dump))

    # Reload config
    config = get_config(config_path)

    return remote_name, remote_rclone_path, config, config_path, data_path


# %%
#|export
def create_repoyards(remote_name="my_remote", num_repoyards=1):
    """Create one or more temporary repoyards for testing.

    This is the legacy function for backwards compatibility.
    Prefer using the temp_repoyard fixture for new tests.

    Args:
        remote_name: Name for the rclone remote
        num_repoyards: Number of repoyards to create (for multi-machine tests)

    Returns:
        If num_repoyards == 1:
            tuple: (remote_name, remote_rclone_path, config, config_path, data_path)
        If num_repoyards > 1:
            tuple: (remote_name, remote_rclone_path, list of (config, config_path, data_path))
    """
    remote_rclone_path = Path(tempfile.mkdtemp(prefix=f"{remote_name}_", dir="/tmp"))

    repoyards = []
    for i in range(num_repoyards):
        test_folder_path = Path(tempfile.mkdtemp(prefix=f"repoyard_{i}_", dir="/tmp"))
        test_folder_path.mkdir(parents=True, exist_ok=True)
        config_path = test_folder_path / ".config" / "repoyard" / "config.toml"
        data_path = test_folder_path / ".repoyard"

        # Run init
        init_repoyard(config_path=config_path, data_path=data_path, verbose=False)
        config = get_config(config_path)

        # Add a storage location
        config_dump = toml.load(config_path)
        config_dump["user_repos_path"] = (test_folder_path / "user_repos").as_posix()
        config_dump["user_repo_groups_path"] = (
            test_folder_path / "user_repo_groups"
        ).as_posix()
        config_dump["storage_locations"][remote_name] = {
            "storage_type": "rclone",
            "store_path": "repoyard",
        }

        # Set up a rclone remote path
        config.rclone_config_path.write_text(
            config.rclone_config_path.read_text()
            + "\n"
            + inspect.cleandoc(f"""
        [{remote_name}]
        type = alias
        remote = {remote_rclone_path}
        """)
        )

        config_path.write_text(toml.dumps(config_dump))

        # Load config
        config = get_config(config_path)

        repoyards.append((config, config_path, data_path))

    if len(repoyards) == 1:
        config, config_path, data_path = repoyards[0]
        return remote_name, remote_rclone_path, config, config_path, data_path
    else:
        return remote_name, remote_rclone_path, repoyards


# ============================================================================
# Command execution utilities
# ============================================================================

# %%
#|export
class CmdFailed(Exception):
    """Exception raised when a shell command fails."""
    pass


def run_cmd(cmd: str, capture_output: bool = True):
    """Run a shell command and return its output.

    Args:
        cmd: Shell command to run
        capture_output: Whether to capture and return stdout

    Returns:
        stdout if capture_output is True

    Raises:
        CmdFailed: If the command exits with non-zero status
    """
    if not capture_output:
        res = subprocess.run(cmd, shell=True)
    else:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode != 0:
        raise CmdFailed(
            f"Command '{cmd}' failed with return code {res.returncode}. "
            f"Stdout:\n{res.stdout}\n\nStderr:\n{res.stderr}"
        )
    if capture_output:
        return res.stdout


def run_cmd_in_background(cmd: str, print_output: bool = False):
    """Run a shell command in the background.

    Args:
        cmd: Shell command to run
        print_output: Whether to show output

    Returns:
        subprocess.Popen instance
    """
    if print_output:
        return subprocess.Popen(cmd, shell=True)
    else:
        return subprocess.Popen(
            cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

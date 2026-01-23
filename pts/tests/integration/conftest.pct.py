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
import shutil
import toml
import inspect
import pytest

from repoyard.cmds import init_repoyard
from repoyard.config import get_config


# ============================================================================
# Fixture config paths
# ============================================================================

def _get_fixture_configs_path() -> Path:
    """Get path to fixture configs, handling both module and notebook contexts."""
    # When running as a module, __file__ is defined
    # Generated conftest.py is at src/tests/integration/conftest.py
    # Fixtures are at tests/fixtures/configs/ (at repo root)
    try:
        return Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "configs"
    except NameError:
        # Running in a notebook - find repo root by looking for pyproject.toml
        cwd = Path.cwd()
        for parent in [cwd] + list(cwd.parents):
            if (parent / "pyproject.toml").exists():
                return parent / "tests" / "fixtures" / "configs"
        raise RuntimeError("Could not find repo root (no pyproject.toml found in parent directories)")

FIXTURE_CONFIGS_PATH = _get_fixture_configs_path()


# ============================================================================
# New isolated test fixtures (recommended)
# ============================================================================

# %%
#|export
def _setup_repoyard_from_fixture(tmp_path: Path, config_name: str):
    """
    Set up an isolated test repoyard using configs from fixtures directory.

    Args:
        tmp_path: Pytest tmp_path for isolation
        config_name: Name of config directory (e.g., "default_local", "default_remote")

    Returns:
        dict with config, config_path, data_path, remote_storage_path, etc.
    """
    fixture_config_dir = FIXTURE_CONFIGS_PATH / config_name

    # Create test directory structure
    test_config_dir = tmp_path / ".config" / "repoyard"
    test_data_dir = tmp_path / ".repoyard"
    test_local_store = tmp_path / "local_store"
    test_remote_storage = tmp_path / "remote_storage"  # For rclone alias
    test_user_repos = tmp_path / "repos"
    test_user_groups = tmp_path / "repo-groups"

    # Create directories that init_repoyard expects and tests need
    test_local_store.mkdir(parents=True)
    test_remote_storage.mkdir(parents=True)
    test_user_repos.mkdir(parents=True)
    test_user_groups.mkdir(parents=True)

    # Run init_repoyard to set up proper structure (creates config_dir, data_dir, etc.)
    config_path = test_config_dir / "config.toml"
    init_repoyard(config_path=config_path, data_path=test_data_dir, verbose=False)

    # Now customize config.toml with our fixture settings
    config_data = toml.load(fixture_config_dir / "config.toml")

    # Override paths to use temp directory
    config_data["repoyard_data_path"] = str(test_data_dir)
    config_data["user_repos_path"] = str(test_user_repos)
    config_data["user_repo_groups_path"] = str(test_user_groups)

    with open(config_path, "w") as f:
        toml.dump(config_data, f)

    # Copy and customize rclone config
    rclone_config_path = test_config_dir / "repoyard_rclone.conf"
    rclone_source = fixture_config_dir / "repoyard_rclone.conf"

    if rclone_source.exists():
        rclone_content = rclone_source.read_text()
        # Replace placeholder paths with actual temp paths
        rclone_content = rclone_content.replace(
            "__REPLACED_AT_RUNTIME__",
            str(test_remote_storage)
        )
        rclone_config_path.write_text(rclone_content)
    else:
        # No rclone config (will be populated by caller for remote tests)
        rclone_config_path.write_text("")

    # Copy exclude file
    exclude_source = fixture_config_dir / "default.rclone_exclude"
    if exclude_source.exists():
        shutil.copy(exclude_source, test_config_dir / "default.rclone_exclude")

    # Load config
    config = get_config(config_path)

    # Get the storage location name from config
    storage_location_name = config_data.get("default_storage_location", "local_test")

    return {
        "config": config,
        "config_path": config_path,
        "data_path": test_data_dir,
        "local_store": test_local_store,
        "remote_storage": test_remote_storage,
        "user_repos": test_user_repos,
        "user_groups": test_user_groups,
        "storage_location_name": storage_location_name,
    }


@pytest.fixture
def test_repoyard_local(tmp_path):
    """
    Create an isolated test repoyard with local-only storage.

    Uses configs from tests/fixtures/configs/default_local/.
    All paths (including locks) are contained within tmp_path.

    Returns:
        dict: {
            "config": Config object,
            "config_path": Path to config.toml,
            "data_path": Path to .repoyard data directory,
            "local_store": Path to local storage,
            "remote_storage": Path to rclone alias target (local dir),
            "user_repos": Path to user repos symlinks,
            "user_groups": Path to user repo groups symlinks,
            "storage_location_name": Name of the storage location,
        }
    """
    return _setup_repoyard_from_fixture(tmp_path, "default_local")


@pytest.fixture
def test_repoyard_remote(tmp_path):
    """
    Create an isolated test repoyard with real remote storage.

    Uses configs from tests/fixtures/configs/default_remote/.

    IMPORTANT: This fixture requires repoyard_rclone.conf to exist in
    tests/fixtures/configs/default_remote/. If it doesn't exist, the test
    is skipped with a helpful message.

    The remote storage uses store_path="repoyard-test" to avoid polluting
    your real repoyard data.

    Returns:
        Same as test_repoyard_local
    """
    fixture_config_dir = FIXTURE_CONFIGS_PATH / "default_remote"
    rclone_conf_path = fixture_config_dir / "repoyard_rclone.conf"

    if not rclone_conf_path.exists():
        pytest.skip(
            "Remote rclone config not found. "
            "To run remote tests, copy repoyard_rclone.conf.template to "
            "repoyard_rclone.conf in tests/fixtures/configs/default_remote/ "
            "and add your credentials."
        )

    result = _setup_repoyard_from_fixture(tmp_path, "default_remote")

    # Copy the actual rclone config (with credentials)
    test_config_dir = result["config_path"].parent
    shutil.copy(rclone_conf_path, test_config_dir / "repoyard_rclone.conf")

    # Reload config to pick up rclone settings
    result["config"] = get_config(result["config_path"])
    result["has_remote"] = True

    return result


# ============================================================================
# Legacy fixtures (for backwards compatibility)
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

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

from boxyard.cmds import init_boxyard
from boxyard.config import get_config


# ============================================================================
# Fixture config paths
# ============================================================================

def _get_fixture_configs_path() -> Path:
    """Get path to fixture configs, handling both module and notebook contexts."""
    # When running as a module, __file__ is defined
    # Generated conftest.py is at src/tests/integration/conftest.py
    # Fixtures are at tests/fixtures/configs/ (at box root)
    try:
        return Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / "configs"
    except NameError:
        # Running in a notebook - find box root by looking for pyproject.toml
        cwd = Path.cwd()
        for parent in [cwd] + list(cwd.parents):
            if (parent / "pyproject.toml").exists():
                return parent / "tests" / "fixtures" / "configs"
        raise RuntimeError("Could not find box root (no pyproject.toml found in parent directories)")

FIXTURE_CONFIGS_PATH = _get_fixture_configs_path()


# ============================================================================
# New isolated test fixtures (recommended)
# ============================================================================

# %%
#|export
def _setup_boxyard_from_fixture(tmp_path: Path, config_name: str):
    """
    Set up an isolated test boxyard using configs from fixtures directory.

    Args:
        tmp_path: Pytest tmp_path for isolation
        config_name: Name of config directory (e.g., "default_local", "default_remote")

    Returns:
        dict with config, config_path, data_path, remote_storage_path, etc.
    """
    fixture_config_dir = FIXTURE_CONFIGS_PATH / config_name

    # Create test directory structure
    test_config_dir = tmp_path / ".config" / "boxyard"
    test_data_dir = tmp_path / ".boxyard"
    test_local_store = tmp_path / "local_store"
    test_remote_storage = tmp_path / "remote_storage"  # For rclone alias
    test_user_boxes = tmp_path / "boxes"
    test_user_groups = tmp_path / "box-groups"

    # Create directories that init_boxyard expects and tests need
    test_local_store.mkdir(parents=True)
    test_remote_storage.mkdir(parents=True)
    test_user_boxes.mkdir(parents=True)
    test_user_groups.mkdir(parents=True)

    # Run init_boxyard to set up proper structure (creates config_dir, data_dir, etc.)
    config_path = test_config_dir / "config.toml"
    init_boxyard(config_path=config_path, data_path=test_data_dir, verbose=False)

    # Now customize config.toml with our fixture settings
    config_data = toml.load(fixture_config_dir / "config.toml")

    # Override paths to use temp directory
    config_data["boxyard_data_path"] = str(test_data_dir)
    config_data["user_boxes_path"] = str(test_user_boxes)
    config_data["user_box_groups_path"] = str(test_user_groups)

    with open(config_path, "w") as f:
        toml.dump(config_data, f)

    # Copy and customize rclone config
    rclone_config_path = test_config_dir / "boxyard_rclone.conf"
    rclone_source = fixture_config_dir / "boxyard_rclone.conf"

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
        "user_boxes": test_user_boxes,
        "user_groups": test_user_groups,
        "storage_location_name": storage_location_name,
    }


@pytest.fixture
def test_boxyard_local(tmp_path):
    """
    Create an isolated test boxyard with local-only storage.

    Uses configs from tests/fixtures/configs/default_local/.
    All paths (including locks) are contained within tmp_path.

    Returns:
        dict: {
            "config": Config object,
            "config_path": Path to config.toml,
            "data_path": Path to .boxyard data directory,
            "local_store": Path to local storage,
            "remote_storage": Path to rclone alias target (local dir),
            "user_boxes": Path to user boxes symlinks,
            "user_groups": Path to user box groups symlinks,
            "storage_location_name": Name of the storage location,
        }
    """
    return _setup_boxyard_from_fixture(tmp_path, "default_local")


@pytest.fixture
def test_boxyard_remote(tmp_path):
    """
    Create an isolated test boxyard with real remote storage.

    Uses configs from tests/fixtures/configs/default_remote/.

    IMPORTANT: This fixture requires boxyard_rclone.conf to exist in
    tests/fixtures/configs/default_remote/. If it doesn't exist, the test
    is skipped with a helpful message.

    The remote storage uses store_path="boxyard-test" to avoid polluting
    your real boxyard data.

    Returns:
        Same as test_boxyard_local
    """
    fixture_config_dir = FIXTURE_CONFIGS_PATH / "default_remote"
    rclone_conf_path = fixture_config_dir / "boxyard_rclone.conf"

    if not rclone_conf_path.exists():
        pytest.skip(
            "Remote rclone config not found. "
            "To run remote tests, copy boxyard_rclone.conf.template to "
            "boxyard_rclone.conf in tests/fixtures/configs/default_remote/ "
            "and add your credentials."
        )

    result = _setup_boxyard_from_fixture(tmp_path, "default_remote")

    # Copy the actual rclone config (with credentials)
    test_config_dir = result["config_path"].parent
    shutil.copy(rclone_conf_path, test_config_dir / "boxyard_rclone.conf")

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
def temp_boxyard(tmp_path):
    """Create a single temporary boxyard for testing.

    Returns:
        tuple: (remote_name, remote_rclone_path, config, config_path, data_path)
    """
    remote_name = "test_remote"
    remote_rclone_path = tmp_path / "remote_storage"
    remote_rclone_path.mkdir()

    test_folder_path = tmp_path / "boxyard"
    test_folder_path.mkdir()

    config_path = test_folder_path / ".config" / "boxyard" / "config.toml"
    data_path = test_folder_path / ".boxyard"

    # Run init
    init_boxyard(config_path=config_path, data_path=data_path, verbose=False)
    config = get_config(config_path)

    # Add a storage location
    config_dump = toml.load(config_path)
    config_dump["user_boxes_path"] = (test_folder_path / "user_boxes").as_posix()
    config_dump["user_box_groups_path"] = (
        test_folder_path / "user_box_groups"
    ).as_posix()
    config_dump["storage_locations"][remote_name] = {
        "storage_type": "rclone",
        "store_path": "boxyard",
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
def create_boxyards(remote_name="my_remote", num_boxyards=1):
    """Create one or more temporary boxyards for testing.

    This is the legacy function for backwards compatibility.
    Prefer using the temp_boxyard fixture for new tests.

    Args:
        remote_name: Name for the rclone remote
        num_boxyards: Number of boxyards to create (for multi-machine tests)

    Returns:
        If num_boxyards == 1:
            tuple: (remote_name, remote_rclone_path, config, config_path, data_path)
        If num_boxyards > 1:
            tuple: (remote_name, remote_rclone_path, list of (config, config_path, data_path))
    """
    remote_rclone_path = Path(tempfile.mkdtemp(prefix=f"{remote_name}_", dir="/tmp"))

    boxyards = []
    for i in range(num_boxyards):
        test_folder_path = Path(tempfile.mkdtemp(prefix=f"boxyard_{i}_", dir="/tmp"))
        test_folder_path.mkdir(parents=True, exist_ok=True)
        config_path = test_folder_path / ".config" / "boxyard" / "config.toml"
        data_path = test_folder_path / ".boxyard"

        # Run init
        init_boxyard(config_path=config_path, data_path=data_path, verbose=False)
        config = get_config(config_path)

        # Add a storage location
        config_dump = toml.load(config_path)
        config_dump["user_boxes_path"] = (test_folder_path / "user_boxes").as_posix()
        config_dump["user_box_groups_path"] = (
            test_folder_path / "user_box_groups"
        ).as_posix()
        config_dump["storage_locations"][remote_name] = {
            "storage_type": "rclone",
            "store_path": "boxyard",
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

        boxyards.append((config, config_path, data_path))

    if len(boxyards) == 1:
        config, config_path, data_path = boxyards[0]
        return remote_name, remote_rclone_path, config, config_path, data_path
    else:
        return remote_name, remote_rclone_path, boxyards


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

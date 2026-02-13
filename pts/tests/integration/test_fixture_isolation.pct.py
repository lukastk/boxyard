# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Test Fixture Isolation
#
# Tests to verify that the new test fixtures properly isolate test environments.

# %%
#|default_exp integration.test_fixture_isolation

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import pytest
from pathlib import Path

from boxyard.cmds import new_box, sync_box, delete_box
from boxyard._models import get_boxyard_meta


# ============================================================================
# Tests using local fixtures
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_local_fixture_creates_isolated_environment(test_boxyard_local):
    """Test that the local fixture creates a fully isolated environment."""
    env = test_boxyard_local

    # Verify all paths exist and are in the tmp directory
    assert env["config_path"].exists()
    assert env["data_path"].exists()
    assert env["local_store"].exists()
    assert env["remote_storage"].exists()

    # Verify paths are not in the user's home directory
    home = Path.home()
    assert not str(env["config_path"]).startswith(str(home / ".config"))
    assert not str(env["data_path"]).startswith(str(home / ".boxyard"))

    # Verify lock directory path is isolated (created on demand when locks are acquired)
    lock_dir = env["data_path"] / "locks"
    assert not str(lock_dir).startswith(str(home))


@pytest.mark.integration
def test_local_fixture_can_create_and_sync_box(test_boxyard_local):
    """Test that we can create and sync a box using the local fixture."""
    import asyncio

    env = test_boxyard_local

    # Create a new box
    box_index_name = new_box(
        config_path=env["config_path"],
        box_name="test_box",
        storage_location=env["storage_location_name"],
    )

    # Verify box was created
    boxyard_meta = get_boxyard_meta(env["config"])
    assert box_index_name in boxyard_meta.by_index_name

    # Sync the box
    asyncio.run(sync_box(
        config_path=env["config_path"],
        box_index_name=box_index_name,
    ))

    # Verify sync completed by checking local box still exists
    # (Remote path structure depends on rclone config which varies)
    box_meta = boxyard_meta.by_index_name[box_index_name]
    local_box_path = box_meta.get_local_path(env["config"])
    assert local_box_path.exists()


@pytest.mark.integration
def test_local_fixture_locks_are_isolated(test_boxyard_local):
    """Test that locks are created in the isolated data directory."""
    import asyncio

    env = test_boxyard_local

    # Create a box (this should acquire a global lock)
    box_index_name = new_box(
        config_path=env["config_path"],
        box_name="lock_test_box",
        storage_location=env["storage_location_name"],
    )

    # Check that any lock files are in the isolated directory
    lock_dir = env["data_path"] / "locks"
    user_lock_dir = Path.home() / ".boxyard" / "locks"

    # The user's lock directory should not have been touched
    # (We can't assert it doesn't exist since it might have existed before)
    # But we can verify our lock directory exists
    assert lock_dir.exists()

    # Clean up
    asyncio.run(delete_box(
        config_path=env["config_path"],
        box_index_name=box_index_name,
    ))


# ============================================================================
# Tests using remote fixtures
# ============================================================================

# %%
#|export
@pytest.mark.integration
@pytest.mark.remote
def test_remote_fixture_skips_without_config(test_boxyard_remote):
    """Test that remote fixture properly loads when config exists.

    Note: This test will be skipped if boxyard_rclone.conf doesn't exist
    in tests/fixtures/configs/default_remote/
    """
    env = test_boxyard_remote

    # If we got here, the config exists
    assert env.get("has_remote", False)
    assert env["config_path"].exists()

    # Verify the storage location uses 'boxyard-test' to avoid pollution
    config = env["config"]
    storage_loc = config.storage_locations.get("test_remote")
    assert storage_loc is not None
    assert storage_loc.store_path == "boxyard-test"

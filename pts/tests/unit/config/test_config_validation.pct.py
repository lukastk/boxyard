# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for Config Validation

# %%
#|default_exp unit.config.test_config_validation

# %%
#|export
import pytest
from pathlib import Path
from pydantic import ValidationError

from boxyard.config import Config, StorageConfig, StorageType


# ============================================================================
# Fixtures
# ============================================================================

# %%
#|export
@pytest.fixture
def valid_config_dict():
    """Create a valid config dictionary for testing."""
    return {
        "config_path": "/tmp/config.toml",
        "default_storage_location": "default",
        "boxyard_data_path": "/tmp/boxyard",
        "box_timestamp_format": "date_and_time",
        "user_boxes_path": "/tmp/boxes",
        "user_box_groups_path": "/tmp/box-groups",
        "storage_locations": {
            "default": {
                "storage_type": "local",
                "store_path": "/tmp/store",
            }
        },
        "box_groups": {},
        "virtual_box_groups": {},
        "default_box_groups": [],
        "box_subid_character_set": "abcdefghijklmnopqrstuvwxyz0123456789",
        "box_subid_length": 5,
        "max_concurrent_rclone_ops": 3,
    }


# ============================================================================
# Tests for storage location name validation
# ============================================================================

# %%
#|export
class TestStorageLocationNameValidation:
    """Tests for storage location name validation."""

    def test_valid_alphanumeric_name(self, valid_config_dict):
        """Alphanumeric storage location names are valid."""
        valid_config_dict["storage_locations"] = {
            "default123": {
                "storage_type": "local",
                "store_path": "/tmp/store",
            }
        }
        valid_config_dict["default_storage_location"] = "default123"
        config = Config(**valid_config_dict)

        assert "default123" in config.storage_locations

    def test_valid_name_with_underscore(self, valid_config_dict):
        """Storage location names with underscores are valid."""
        valid_config_dict["storage_locations"] = {
            "my_storage": {
                "storage_type": "local",
                "store_path": "/tmp/store",
            }
        }
        valid_config_dict["default_storage_location"] = "my_storage"
        config = Config(**valid_config_dict)

        assert "my_storage" in config.storage_locations

    def test_valid_name_with_dash(self, valid_config_dict):
        """Storage location names with dashes are valid."""
        valid_config_dict["storage_locations"] = {
            "my-storage": {
                "storage_type": "local",
                "store_path": "/tmp/store",
            }
        }
        valid_config_dict["default_storage_location"] = "my-storage"
        config = Config(**valid_config_dict)

        assert "my-storage" in config.storage_locations

    def test_invalid_name_with_space(self, valid_config_dict):
        """Storage location names with spaces are invalid."""
        valid_config_dict["storage_locations"] = {
            "my storage": {
                "storage_type": "local",
                "store_path": "/tmp/store",
            }
        }
        valid_config_dict["default_storage_location"] = "my storage"

        with pytest.raises(ValidationError, match="invalid"):
            Config(**valid_config_dict)

    def test_invalid_name_with_special_chars(self, valid_config_dict):
        """Storage location names with special characters are invalid."""
        invalid_names = ["storage@home", "storage#1", "storage.backup", "storage/main"]

        for name in invalid_names:
            valid_config_dict["storage_locations"] = {
                name: {
                    "storage_type": "local",
                    "store_path": "/tmp/store",
                }
            }
            valid_config_dict["default_storage_location"] = name

            with pytest.raises(ValidationError, match="invalid"):
                Config(**valid_config_dict)


# ============================================================================
# Tests for default storage location validation
# ============================================================================

# %%
#|export
class TestDefaultStorageLocationValidation:
    """Tests for default_storage_location validation."""

    def test_default_storage_must_exist(self, valid_config_dict):
        """default_storage_location must exist in storage_locations."""
        valid_config_dict["default_storage_location"] = "nonexistent"

        with pytest.raises(ValidationError, match="not found"):
            Config(**valid_config_dict)

    def test_default_storage_exists(self, valid_config_dict):
        """Valid default_storage_location is accepted."""
        config = Config(**valid_config_dict)
        assert config.default_storage_location == "default"
        assert config.default_storage_location in config.storage_locations


# ============================================================================
# Tests for storage locations requirement
# ============================================================================

# %%
#|export
class TestStorageLocationsRequired:
    """Tests for storage locations requirement."""

    def test_no_storage_locations_raises(self, valid_config_dict):
        """Empty storage_locations raises error."""
        valid_config_dict["storage_locations"] = {}

        with pytest.raises(ValidationError, match="No storage locations"):
            Config(**valid_config_dict)

    def test_at_least_one_storage_location(self, valid_config_dict):
        """At least one storage location is required."""
        config = Config(**valid_config_dict)
        assert len(config.storage_locations) >= 1


# ============================================================================
# Tests for group name validation
# ============================================================================

# %%
#|export
class TestGroupNameValidation:
    """Tests for box_groups and virtual_box_groups name validation."""

    def test_valid_group_names(self, valid_config_dict):
        """Valid group names are accepted."""
        valid_config_dict["box_groups"] = {
            "backend": {},
            "frontend": {},
            "my-group": {},
            "my_group": {},
            "category/subcategory": {},
        }
        config = Config(**valid_config_dict)

        assert len(config.box_groups) == 5

    def test_invalid_group_name_with_space(self, valid_config_dict):
        """Group names with spaces are invalid."""
        valid_config_dict["box_groups"] = {
            "my group": {},
        }

        with pytest.raises(ValidationError, match="Invalid group name"):
            Config(**valid_config_dict)

    def test_invalid_group_name_with_special_chars(self, valid_config_dict):
        """Group names with special characters are invalid."""
        invalid_names = ["group@test", "group#1", "group$"]

        for name in invalid_names:
            valid_config_dict["box_groups"] = {
                name: {},
            }

            with pytest.raises(ValidationError, match="Invalid group name"):
                Config(**valid_config_dict)

    def test_virtual_group_names_validated(self, valid_config_dict):
        """Virtual group names are also validated."""
        valid_config_dict["virtual_box_groups"] = {
            "invalid group": {
                "filter_expr": "backend",
            },
        }

        with pytest.raises(ValidationError, match="Invalid group name"):
            Config(**valid_config_dict)

    def test_valid_virtual_group_names(self, valid_config_dict):
        """Valid virtual group names are accepted."""
        valid_config_dict["virtual_box_groups"] = {
            "active-work": {
                "filter_expr": "backend AND NOT archived",
            },
        }
        config = Config(**valid_config_dict)

        assert "active-work" in config.virtual_box_groups


# ============================================================================
# Tests for required fields
# ============================================================================

# %%
#|export
class TestRequiredFields:
    """Tests for required field validation."""

    def test_missing_default_storage_location(self, valid_config_dict):
        """Missing default_storage_location raises error."""
        del valid_config_dict["default_storage_location"]

        with pytest.raises(ValidationError):
            Config(**valid_config_dict)

    def test_missing_boxyard_data_path(self, valid_config_dict):
        """Missing boxyard_data_path raises error."""
        del valid_config_dict["boxyard_data_path"]

        with pytest.raises(ValidationError):
            Config(**valid_config_dict)

    def test_missing_user_boxes_path(self, valid_config_dict):
        """Missing user_boxes_path raises error."""
        del valid_config_dict["user_boxes_path"]

        with pytest.raises(ValidationError):
            Config(**valid_config_dict)

    def test_missing_storage_locations(self, valid_config_dict):
        """Missing storage_locations raises error."""
        del valid_config_dict["storage_locations"]

        with pytest.raises(ValidationError):
            Config(**valid_config_dict)


# ============================================================================
# Tests for extra fields (strict mode)
# ============================================================================

# %%
#|export
class TestStrictMode:
    """Tests for strict mode (extra fields forbidden)."""

    def test_extra_field_raises_error(self, valid_config_dict):
        """Extra fields in config raise error due to strict mode."""
        valid_config_dict["unknown_field"] = "value"

        with pytest.raises(ValidationError, match="extra"):
            Config(**valid_config_dict)


# ============================================================================
# Tests for multiple storage locations
# ============================================================================

# %%
#|export
class TestMultipleStorageLocations:
    """Tests for configs with multiple storage locations."""

    def test_multiple_storage_locations(self, valid_config_dict):
        """Multiple storage locations can be configured."""
        valid_config_dict["storage_locations"] = {
            "default": {
                "storage_type": "local",
                "store_path": "/tmp/store",
            },
            "backup": {
                "storage_type": "rclone",
                "store_path": "remote:backup",
            },
            "archive": {
                "storage_type": "local",
                "store_path": "/mnt/archive",
            },
        }
        config = Config(**valid_config_dict)

        assert len(config.storage_locations) == 3
        assert config.storage_locations["default"].storage_type == StorageType.LOCAL
        assert config.storage_locations["backup"].storage_type == StorageType.RCLONE
        assert config.storage_locations["archive"].storage_type == StorageType.LOCAL

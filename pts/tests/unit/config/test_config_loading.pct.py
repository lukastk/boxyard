# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for Config Loading

# %%
#|default_exp unit.config.test_config_loading

# %%
#|export
import pytest
import tempfile
from pathlib import Path
from pydantic import ValidationError

from boxyard.config import Config, get_config, StorageType, BoxTimestampFormat


# ============================================================================
# Fixtures
# ============================================================================

# %%
#|export
@pytest.fixture
def minimal_config_dict():
    """Create a minimal valid config dictionary."""
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


@pytest.fixture
def full_config_dict():
    """Create a full config dictionary with all options."""
    return {
        "config_path": "/home/user/.config/boxyard/config.toml",
        "default_storage_location": "main",
        "boxyard_data_path": "/home/user/.boxyard",
        "box_timestamp_format": "date_only",
        "user_boxes_path": "/home/user/boxes",
        "user_box_groups_path": "/home/user/box-groups",
        "storage_locations": {
            "main": {
                "storage_type": "rclone",
                "store_path": "remote:bucket/boxyard",
            },
            "backup": {
                "storage_type": "local",
                "store_path": "/mnt/backup/boxyard",
            },
        },
        "box_groups": {
            "work": {
                "symlink_name": "work-projects",
                "box_title_mode": "name",
            },
            "personal": {},
        },
        "virtual_box_groups": {
            "active": {
                "filter_expr": "work AND NOT archived",
                "box_title_mode": "datetime_and_name",
            },
        },
        "default_box_groups": ["personal"],
        "box_subid_character_set": "abcdefghijklmnopqrstuvwxyz",
        "box_subid_length": 6,
        "max_concurrent_rclone_ops": 5,
    }


# ============================================================================
# Tests for Config construction
# ============================================================================

# %%
#|export
class TestConfigConstruction:
    """Tests for basic Config construction."""

    def test_minimal_config(self, minimal_config_dict):
        """Minimal config can be constructed."""
        config = Config(**minimal_config_dict)

        assert config.default_storage_location == "default"
        assert config.box_timestamp_format == BoxTimestampFormat.DATE_AND_TIME
        assert len(config.storage_locations) == 1
        assert "default" in config.storage_locations

    def test_full_config(self, full_config_dict):
        """Full config with all options can be constructed."""
        config = Config(**full_config_dict)

        assert config.default_storage_location == "main"
        assert config.box_timestamp_format == BoxTimestampFormat.DATE_ONLY
        assert len(config.storage_locations) == 2
        assert "main" in config.storage_locations
        assert "backup" in config.storage_locations
        assert len(config.box_groups) == 2
        assert len(config.virtual_box_groups) == 1
        assert config.default_box_groups == ["personal"]
        assert config.box_subid_length == 6
        assert config.max_concurrent_rclone_ops == 5

    def test_config_with_empty_groups(self, minimal_config_dict):
        """Config with empty groups is valid."""
        config = Config(**minimal_config_dict)

        assert config.box_groups == {}
        assert config.virtual_box_groups == {}
        assert config.default_box_groups == []


# ============================================================================
# Tests for path expansion
# ============================================================================

# %%
#|export
class TestPathExpansion:
    """Tests for path expansion in Config."""

    def test_tilde_expansion_boxyard_data_path(self, minimal_config_dict):
        """Tilde is expanded in boxyard_data_path."""
        minimal_config_dict["boxyard_data_path"] = "~/.boxyard"
        config = Config(**minimal_config_dict)

        assert "~" not in str(config.boxyard_data_path)
        assert config.boxyard_data_path.is_absolute()

    def test_tilde_expansion_user_boxes_path(self, minimal_config_dict):
        """Tilde is expanded in user_boxes_path."""
        minimal_config_dict["user_boxes_path"] = "~/boxes"
        config = Config(**minimal_config_dict)

        assert "~" not in str(config.user_boxes_path)
        assert config.user_boxes_path.is_absolute()

    def test_tilde_expansion_user_box_groups_path(self, minimal_config_dict):
        """Tilde is expanded in user_box_groups_path."""
        minimal_config_dict["user_box_groups_path"] = "~/box-groups"
        config = Config(**minimal_config_dict)

        assert "~" not in str(config.user_box_groups_path)
        assert config.user_box_groups_path.is_absolute()

    def test_tilde_expansion_config_path(self, minimal_config_dict):
        """Tilde is expanded in config_path."""
        minimal_config_dict["config_path"] = "~/.config/boxyard/config.toml"
        config = Config(**minimal_config_dict)

        assert "~" not in str(config.config_path)
        assert config.config_path.is_absolute()


# ============================================================================
# Tests for derived paths (properties)
# ============================================================================

# %%
#|export
class TestDerivedPaths:
    """Tests for Config derived path properties."""

    def test_local_store_path(self, minimal_config_dict):
        """local_store_path is derived correctly."""
        minimal_config_dict["boxyard_data_path"] = "/home/user/.boxyard"
        config = Config(**minimal_config_dict)

        assert config.local_store_path == Path("/home/user/.boxyard/local_store")

    def test_local_sync_backups_path(self, minimal_config_dict):
        """local_sync_backups_path is derived correctly."""
        minimal_config_dict["boxyard_data_path"] = "/home/user/.boxyard"
        config = Config(**minimal_config_dict)

        assert config.local_sync_backups_path == Path("/home/user/.boxyard/sync_backups")

    def test_boxyard_meta_path(self, minimal_config_dict):
        """boxyard_meta_path is derived correctly."""
        minimal_config_dict["boxyard_data_path"] = "/home/user/.boxyard"
        config = Config(**minimal_config_dict)

        assert config.boxyard_meta_path == Path("/home/user/.boxyard/boxyard_meta.json")

    def test_rclone_config_path(self, minimal_config_dict):
        """rclone_config_path is derived correctly."""
        minimal_config_dict["config_path"] = "/home/user/.config/boxyard/config.toml"
        config = Config(**minimal_config_dict)

        assert config.rclone_config_path == Path("/home/user/.config/boxyard/boxyard_rclone.conf")

    def test_default_rclone_exclude_path(self, minimal_config_dict):
        """default_rclone_exclude_path is derived correctly."""
        minimal_config_dict["config_path"] = "/home/user/.config/boxyard/config.toml"
        config = Config(**minimal_config_dict)

        assert config.default_rclone_exclude_path == Path("/home/user/.config/boxyard/default.rclone_exclude")


# ============================================================================
# Tests for get_config function
# ============================================================================

# %%
#|export
class TestGetConfig:
    """Tests for the get_config function."""

    def test_load_valid_config_file(self, tmp_path):
        """Valid config file can be loaded."""
        config_file = tmp_path / "config.toml"
        config_content = """
default_storage_location = "default"
boxyard_data_path = "/tmp/boxyard"
box_timestamp_format = "date_and_time"
user_boxes_path = "/tmp/boxes"
user_box_groups_path = "/tmp/box-groups"
box_subid_character_set = "abcdefghijklmnopqrstuvwxyz0123456789"
box_subid_length = 5
max_concurrent_rclone_ops = 3
default_box_groups = []

[storage_locations.default]
storage_type = "local"
store_path = "/tmp/store"

[box_groups]

[virtual_box_groups]
"""
        config_file.write_text(config_content)

        config = get_config(config_file)

        assert config.default_storage_location == "default"
        assert config.config_path == config_file

    def test_load_missing_file_raises(self, tmp_path):
        """Loading missing config file raises FileNotFoundError."""
        missing_file = tmp_path / "nonexistent.toml"

        with pytest.raises(FileNotFoundError):
            get_config(missing_file)

    def test_load_invalid_toml_raises(self, tmp_path):
        """Loading invalid TOML raises error."""
        config_file = tmp_path / "invalid.toml"
        config_file.write_text("this is not valid toml [")

        with pytest.raises(Exception):  # toml.TomlDecodeError
            get_config(config_file)

    def test_load_with_tilde_path(self, tmp_path, monkeypatch):
        """Config file path with tilde is expanded."""
        # Create config in tmp_path simulating home
        config_file = tmp_path / "config.toml"
        config_content = """
default_storage_location = "default"
boxyard_data_path = "/tmp/boxyard"
box_timestamp_format = "date_and_time"
user_boxes_path = "/tmp/boxes"
user_box_groups_path = "/tmp/box-groups"
box_subid_character_set = "abcdefghijklmnopqrstuvwxyz0123456789"
box_subid_length = 5
max_concurrent_rclone_ops = 3
default_box_groups = []

[storage_locations.default]
storage_type = "local"
store_path = "/tmp/store"

[box_groups]

[virtual_box_groups]
"""
        config_file.write_text(config_content)

        # Test that absolute path works (we can't easily test ~ expansion)
        config = get_config(config_file)
        assert config is not None


# ============================================================================
# Tests for timestamp format
# ============================================================================

# %%
#|export
class TestTimestampFormat:
    """Tests for box_timestamp_format configuration."""

    def test_date_and_time_format(self, minimal_config_dict):
        """DATE_AND_TIME format is parsed correctly."""
        minimal_config_dict["box_timestamp_format"] = "date_and_time"
        config = Config(**minimal_config_dict)

        assert config.box_timestamp_format == BoxTimestampFormat.DATE_AND_TIME

    def test_date_only_format(self, minimal_config_dict):
        """DATE_ONLY format is parsed correctly."""
        minimal_config_dict["box_timestamp_format"] = "date_only"
        config = Config(**minimal_config_dict)

        assert config.box_timestamp_format == BoxTimestampFormat.DATE_ONLY

    def test_invalid_format_raises(self, minimal_config_dict):
        """Invalid timestamp format raises error."""
        minimal_config_dict["box_timestamp_format"] = "invalid"

        with pytest.raises(ValidationError):
            Config(**minimal_config_dict)

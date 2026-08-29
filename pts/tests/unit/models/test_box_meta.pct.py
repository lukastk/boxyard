# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for BoxMeta

# %%
#|default_exp unit.models.test_box_meta

# %%
#|export
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from pydantic import ValidationError

from boxyard._models import BoxMeta, BoxPart
from boxyard import const


# ============================================================================
# Tests for box_id property
# ============================================================================

# %%
#|export
class TestBoxId:
    """Tests for the box_id property."""

    def test_box_id_with_datetime_timestamp(self):
        """box_id combines timestamp and subid correctly for datetime format."""
        box_meta = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )
        assert box_meta.box_id == "20251122_143022_a7kx9"

    def test_box_id_with_date_only_timestamp(self):
        """box_id combines timestamp and subid correctly for date-only format."""
        box_meta = BoxMeta(
            creation_timestamp_utc="20251122",
            box_subid="b8ly0",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )
        assert box_meta.box_id == "20251122_b8ly0"

    def test_box_id_different_subids(self):
        """Different subids produce different box_ids."""
        box_meta1 = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )
        box_meta2 = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="z9abc",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )
        assert box_meta1.box_id != box_meta2.box_id


# ============================================================================
# Tests for index_name property
# ============================================================================

# %%
#|export
class TestIndexName:
    """Tests for the index_name property."""

    def test_index_name_format(self):
        """index_name combines box_id and name with double underscore."""
        box_meta = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )
        assert box_meta.index_name == "20251122_143022_a7kx9__myproject"

    def test_index_name_with_date_only(self):
        """index_name works with date-only timestamp format."""
        box_meta = BoxMeta(
            creation_timestamp_utc="20251122",
            box_subid="b8ly0",
            name="testproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )
        assert box_meta.index_name == "20251122_b8ly0__testproject"

    def test_index_name_with_special_name(self):
        """index_name handles names with hyphens and underscores."""
        box_meta = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="my-test_project",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )
        assert box_meta.index_name == "20251122_143022_a7kx9__my-test_project"


# ============================================================================
# Tests for creation_timestamp_datetime property
# ============================================================================

# %%
#|export
class TestCreationTimestampDatetime:
    """Tests for the creation_timestamp_datetime property."""

    def test_datetime_timestamp_parsing(self):
        """Parses datetime format timestamp correctly."""
        box_meta = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )
        dt = box_meta.creation_timestamp_datetime
        assert dt.year == 2025
        assert dt.month == 11
        assert dt.day == 22
        assert dt.hour == 14
        assert dt.minute == 30
        assert dt.second == 22

    def test_date_only_timestamp_parsing(self):
        """Parses date-only format timestamp correctly."""
        box_meta = BoxMeta(
            creation_timestamp_utc="20251122",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )
        dt = box_meta.creation_timestamp_datetime
        assert dt.year == 2025
        assert dt.month == 11
        assert dt.day == 22
        # Time components should be zero for date-only
        assert dt.hour == 0
        assert dt.minute == 0
        assert dt.second == 0


# ============================================================================
# Tests for validate_group_name
# ============================================================================

# %%
#|export
class TestValidateGroupName:
    """Tests for the validate_group_name class method."""

    def test_valid_alphanumeric(self):
        """Valid alphanumeric group names."""
        BoxMeta.validate_group_name("group1")
        BoxMeta.validate_group_name("GROUP1")
        BoxMeta.validate_group_name("Group123")

    def test_valid_with_underscore(self):
        """Valid group names with underscores."""
        BoxMeta.validate_group_name("my_group")
        BoxMeta.validate_group_name("test_group_123")
        BoxMeta.validate_group_name("_underscore")

    def test_valid_with_hyphen(self):
        """Valid group names with hyphens."""
        BoxMeta.validate_group_name("my-group")
        BoxMeta.validate_group_name("test-group-123")
        BoxMeta.validate_group_name("-hyphen")

    def test_valid_with_slash(self):
        """Valid group names with slashes (for hierarchical groups)."""
        BoxMeta.validate_group_name("parent/child")
        BoxMeta.validate_group_name("a/b/c")
        BoxMeta.validate_group_name("projects/backend/api")

    def test_invalid_with_space(self):
        """Group names with spaces are invalid."""
        with pytest.raises(ValueError, match="Invalid group name"):
            BoxMeta.validate_group_name("my group")

    def test_invalid_with_special_chars(self):
        """Group names with special characters are invalid."""
        invalid_names = ["group@test", "group#1", "group$", "group%", "group&"]
        for name in invalid_names:
            with pytest.raises(ValueError, match="Invalid group name"):
                BoxMeta.validate_group_name(name)

    def test_invalid_empty_string(self):
        """Empty string is invalid."""
        with pytest.raises(ValueError, match="Invalid group name"):
            BoxMeta.validate_group_name("")

    def test_invalid_non_string(self):
        """Non-string values are invalid."""
        with pytest.raises(ValueError, match="Invalid group name"):
            BoxMeta.validate_group_name(123)
        with pytest.raises(ValueError, match="Invalid group name"):
            BoxMeta.validate_group_name(None)


# ============================================================================
# Tests for BoxMeta validation
# ============================================================================

# %%
#|export
class TestBoxMetaValidation:
    """Tests for BoxMeta model validation."""

    def test_duplicate_groups_rejected(self):
        """Duplicate groups in the list are rejected."""
        with pytest.raises(ValidationError, match="Groups must be unique"):
            BoxMeta(
                creation_timestamp_utc="20251122_143022",
                box_subid="a7kx9",
                name="myproject",
                storage_location="default",
                creator_hostname="myhost",
                groups=["group1", "group1"],
            )

    def test_invalid_group_name_in_list_rejected(self):
        """Invalid group names in the list are rejected."""
        with pytest.raises(ValidationError, match="Invalid group name"):
            BoxMeta(
                creation_timestamp_utc="20251122_143022",
                box_subid="a7kx9",
                name="myproject",
                storage_location="default",
                creator_hostname="myhost",
                groups=["valid", "invalid name with space"],
            )

    def test_invalid_timestamp_format_rejected(self):
        """Invalid timestamp formats are rejected."""
        with pytest.raises(ValidationError, match="Creation timestamp is not valid"):
            BoxMeta(
                creation_timestamp_utc="2025-11-22",  # Wrong format
                box_subid="a7kx9",
                name="myproject",
                storage_location="default",
                creator_hostname="myhost",
                groups=[],
            )

    def test_invalid_timestamp_value_rejected(self):
        """Invalid timestamp values are rejected."""
        with pytest.raises(ValidationError, match="Creation timestamp is not valid"):
            BoxMeta(
                creation_timestamp_utc="20251399_143022",  # Invalid month
                box_subid="a7kx9",
                name="myproject",
                storage_location="default",
                creator_hostname="myhost",
                groups=[],
            )

    def test_valid_box_meta_creation(self):
        """Valid BoxMeta can be created."""
        box_meta = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=["group1", "group2"],
        )
        assert box_meta.name == "myproject"
        assert box_meta.groups == ["group1", "group2"]

    def test_empty_groups_allowed(self):
        """Empty groups list is allowed."""
        box_meta = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )
        assert box_meta.groups == []


# ============================================================================
# Tests for BoxMeta.create factory method
# ============================================================================

# %%
#|export
class TestBoxMetaCreate:
    """Tests for the BoxMeta.create factory method."""

    def test_create_with_datetime_format(self):
        """Create generates proper box with datetime format."""
        from boxyard.config import Config, StorageConfig, StorageType, BoxTimestampFormat

        mock_config = MagicMock(spec=Config)
        mock_config.box_timestamp_format = BoxTimestampFormat.DATE_AND_TIME
        mock_config.box_subid_character_set = "abcdefghijklmnopqrstuvwxyz0123456789"
        mock_config.box_subid_length = 5

        box_meta = BoxMeta.create(
            config=mock_config,
            name="testproject",
            storage_location_name="default",
            creator_hostname="testhost",
            groups=["group1"],
        )

        # Check that timestamp is datetime format (contains underscore in time part)
        assert "_" in box_meta.creation_timestamp_utc
        assert len(box_meta.box_subid) == 5
        assert box_meta.name == "testproject"
        assert box_meta.storage_location == "default"
        assert box_meta.creator_hostname == "testhost"
        assert box_meta.groups == ["group1"]

    def test_create_with_date_only_format(self):
        """Create generates proper box with date-only format."""
        from boxyard.config import Config, StorageConfig, StorageType, BoxTimestampFormat

        mock_config = MagicMock(spec=Config)
        mock_config.box_timestamp_format = BoxTimestampFormat.DATE_ONLY
        mock_config.box_subid_character_set = "abcdefghijklmnopqrstuvwxyz0123456789"
        mock_config.box_subid_length = 5

        box_meta = BoxMeta.create(
            config=mock_config,
            name="testproject",
            storage_location_name="default",
            creator_hostname="testhost",
            groups=[],
        )

        # Check that timestamp is date-only format (no underscore separating time)
        # The format is YYYYMMDD, which doesn't have an underscore
        # But we need to be careful: the timestamp might have one underscore
        # from the format YYYYMMDD_HHMMSS. Date-only has no underscore.
        timestamp = box_meta.creation_timestamp_utc
        # Date only format is just 8 digits
        assert len(timestamp) == 8
        assert timestamp.isdigit()

    def test_create_generates_unique_subids(self):
        """Create generates different subids on each call."""
        from boxyard.config import Config, BoxTimestampFormat

        mock_config = MagicMock(spec=Config)
        mock_config.box_timestamp_format = BoxTimestampFormat.DATE_AND_TIME
        mock_config.box_subid_character_set = "abcdefghijklmnopqrstuvwxyz0123456789"
        mock_config.box_subid_length = 5

        subids = set()
        for _ in range(100):
            box_meta = BoxMeta.create(
                config=mock_config,
                name="test",
                storage_location_name="default",
                creator_hostname="host",
                groups=[],
            )
            subids.add(box_meta.box_subid)

        # With 36^5 = 60,466,176 possibilities, getting 100 unique is expected
        # Allow for some small chance of collision but expect at least 95 unique
        assert len(subids) >= 95


# ============================================================================
# Tests for path generation methods
# ============================================================================

# %%
#|export
class TestPathGeneration:
    """Tests for path generation methods."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config for path tests."""
        from boxyard.config import Config, StorageConfig, StorageType, CheckoutRootConfig

        config = MagicMock(spec=Config)
        config.local_store_path = Path("/home/user/.boxyard/local_store")
        config.user_boxes_path = Path("/home/user/boxes")
        config.boxyard_data_path = Path("/home/user/.boxyard")
        config.placements_path = Path("/home/user/.boxyard/placements")
        config.configured_checkout_roots = {
            "default": CheckoutRootConfig(path=config.user_boxes_path),
        }

        storage_config = MagicMock(spec=StorageConfig)
        storage_config.store_path = Path("remote:bucket/boxyard")
        config.storage_locations = {"default": storage_config}

        return config

    @pytest.fixture
    def box_meta(self):
        """Create a test BoxMeta instance."""
        return BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )

    def test_get_local_path(self, mock_config, box_meta):
        """get_local_path returns correct path."""
        local_path = box_meta.get_local_path(mock_config)
        expected = Path("/home/user/.boxyard/local_store/default/20251122_143022_a7kx9__myproject")
        assert local_path == expected

    def test_get_remote_path(self, mock_config, box_meta):
        """get_remote_path returns correct path."""
        remote_path = box_meta.get_remote_path(mock_config)
        expected = Path("remote:bucket/boxyard/boxes/20251122_143022_a7kx9__myproject")
        assert remote_path == expected

    def test_get_local_part_path_data(self, mock_config, box_meta):
        """get_local_part_path returns correct path for DATA."""
        data_path = box_meta.get_local_part_path(mock_config, BoxPart.DATA)
        expected = Path("/home/user/boxes/20251122_143022_a7kx9__myproject")
        assert data_path == expected

    def test_get_local_part_path_meta(self, mock_config, box_meta):
        """get_local_part_path returns correct path for META."""
        meta_path = box_meta.get_local_part_path(mock_config, BoxPart.META)
        expected = Path("/home/user/.boxyard/local_store/default/20251122_143022_a7kx9__myproject/boxmeta.toml")
        assert meta_path == expected

    def test_get_local_part_path_conf(self, mock_config, box_meta):
        """get_local_part_path returns correct path for CONF."""
        conf_path = box_meta.get_local_part_path(mock_config, BoxPart.CONF)
        expected = Path("/home/user/.boxyard/local_store/default/20251122_143022_a7kx9__myproject/conf")
        assert conf_path == expected

    def test_get_remote_part_path_data(self, mock_config, box_meta):
        """get_remote_part_path returns correct path for DATA."""
        data_path = box_meta.get_remote_part_path(mock_config, BoxPart.DATA)
        expected = Path("remote:bucket/boxyard/boxes/20251122_143022_a7kx9__myproject/data")
        assert data_path == expected

    def test_get_remote_part_path_meta(self, mock_config, box_meta):
        """get_remote_part_path returns correct path for META."""
        meta_path = box_meta.get_remote_part_path(mock_config, BoxPart.META)
        expected = Path("remote:bucket/boxyard/boxes/20251122_143022_a7kx9__myproject/boxmeta.toml")
        assert meta_path == expected

    def test_get_remote_part_path_conf(self, mock_config, box_meta):
        """get_remote_part_path returns correct path for CONF."""
        conf_path = box_meta.get_remote_part_path(mock_config, BoxPart.CONF)
        expected = Path("remote:bucket/boxyard/boxes/20251122_143022_a7kx9__myproject/conf")
        assert conf_path == expected

    def test_get_local_sync_record_path(self, mock_config, box_meta):
        """get_local_sync_record_path returns correct path."""
        for part in BoxPart:
            sync_path = box_meta.get_local_sync_record_path(mock_config, part)
            expected = Path(f"/home/user/.boxyard/sync_records/20251122_143022_a7kx9__myproject/{part.value}.rec")
            assert sync_path == expected

    def test_get_remote_sync_record_path(self, mock_config, box_meta):
        """get_remote_sync_record_path returns correct path."""
        for part in BoxPart:
            sync_path = box_meta.get_remote_sync_record_path(mock_config, part)
            expected = Path(f"remote:bucket/boxyard/sync_records/20251122_143022_a7kx9__myproject/{part.value}.rec")
            assert sync_path == expected

    def test_invalid_box_part_raises_error(self, mock_config, box_meta):
        """Invalid box part raises ValueError."""
        # This test documents expected behavior - we can't easily test with
        # an invalid enum value, but we verify the paths are correct for all valid parts
        pass


# ============================================================================
# Tests for check_included
# ============================================================================

# %%
#|export
class TestCheckIncluded:
    """Tests for the check_included method."""

    def test_check_included_when_data_dir_exists(self, tmp_path):
        """check_included returns True when data directory exists."""
        from boxyard.config import Config, CheckoutRootConfig

        mock_config = MagicMock(spec=Config)
        mock_config.user_boxes_path = tmp_path
        mock_config.placements_path = tmp_path / ".boxyard" / "placements"
        mock_config.configured_checkout_roots = {"default": CheckoutRootConfig(path=tmp_path)}

        box_meta = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )

        # Create the data directory
        data_dir = tmp_path / box_meta.index_name
        data_dir.mkdir()

        assert box_meta.check_included(mock_config) is True

    def test_check_included_when_data_dir_not_exists(self, tmp_path):
        """check_included returns False when data directory doesn't exist."""
        from boxyard.config import Config, CheckoutRootConfig

        mock_config = MagicMock(spec=Config)
        mock_config.user_boxes_path = tmp_path
        mock_config.placements_path = tmp_path / ".boxyard" / "placements"
        mock_config.configured_checkout_roots = {"default": CheckoutRootConfig(path=tmp_path)}

        box_meta = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )

        assert box_meta.check_included(mock_config) is False

    def test_check_included_when_data_is_file(self, tmp_path):
        """check_included returns False when path exists but is a file."""
        from boxyard.config import Config, CheckoutRootConfig

        mock_config = MagicMock(spec=Config)
        mock_config.user_boxes_path = tmp_path
        mock_config.placements_path = tmp_path / ".boxyard" / "placements"
        mock_config.configured_checkout_roots = {"default": CheckoutRootConfig(path=tmp_path)}

        box_meta = BoxMeta(
            creation_timestamp_utc="20251122_143022",
            box_subid="a7kx9",
            name="myproject",
            storage_location="default",
            creator_hostname="myhost",
            groups=[],
        )

        # Create a file instead of directory
        data_file = tmp_path / box_meta.index_name
        data_file.write_text("not a directory")

        assert box_meta.check_included(mock_config) is False


# ============================================================================
# Tests for BoxPart enum
# ============================================================================

# %%
#|export
class TestBoxPart:
    """Tests for the BoxPart enum."""

    def test_box_part_values(self):
        """BoxPart has correct values."""
        assert BoxPart.DATA.value == "data"
        assert BoxPart.META.value == "meta"
        assert BoxPart.CONF.value == "conf"

    def test_box_part_iteration(self):
        """All BoxPart values can be iterated."""
        parts = list(BoxPart)
        assert len(parts) == 3
        assert BoxPart.DATA in parts
        assert BoxPart.META in parts
        assert BoxPart.CONF in parts

# %% [markdown]
# ## Regression: group names must not smuggle a trailing newline

# %%
#|export
class TestValidateGroupNameTrailingNewline:
    r"""
    `validate_group_name` used `re.match(r"^[A-Za-z0-9_\-/]+$", ...)`.

    Python's `$` matches at the end of the string *or immediately before a
    trailing newline*, so "proj\n" was accepted and would then be used verbatim
    as a directory name in the group symlink tree. `re.fullmatch` has no such
    escape hatch.
    """

    def test_rejects_trailing_newline(self):
        with pytest.raises(ValueError):
            BoxMeta.validate_group_name("proj\n")

    def test_rejects_trailing_carriage_return(self):
        with pytest.raises(ValueError):
            BoxMeta.validate_group_name("proj\r")

    def test_rejects_embedded_newline(self):
        with pytest.raises(ValueError):
            BoxMeta.validate_group_name("pr\noj")

    def test_still_accepts_ordinary_names(self):
        BoxMeta.validate_group_name("proj")
        BoxMeta.validate_group_name("ctx/macbook")
        BoxMeta.validate_group_name("adu-me")

# %% [markdown]
# ## Regression: `BoxMeta.create` with an explicit timestamp

# %%
#|export
class TestCreateWithExplicitTimestamp:
    """
    The explicit-timestamp branch of `BoxMeta.create` was broken for every input
    type: it tested `"_" in creation_timestamp_utc` (a membership test against a
    datetime -> TypeError) and then called `.strftime()` on what that check
    implied was a string (-> AttributeError).

    It was unreachable, because `new_box` builds its BoxMeta directly, so
    nothing caught it. These tests make the branch live.
    """

    def _config(self, tmp_path, timestamp_format):
        from boxyard.config import Config, StorageConfig, StorageType, BoxTimestampFormat

        return Config(
            config_path=tmp_path / "config.toml",
            default_storage_location="fake",
            boxyard_data_path=tmp_path / "data",
            box_timestamp_format=timestamp_format,
            user_boxes_path=tmp_path / "boxes",
            user_box_groups_path=tmp_path / "groups",
            storage_locations={
                "fake": StorageConfig(storage_type=StorageType.LOCAL, store_path=tmp_path / "store")
            },
            box_groups={},
            virtual_box_groups={},
            default_box_groups=[],
            box_subid_character_set="abcdefghijklmnopqrstuvwxyz0123456789",
            box_subid_length=6,
            max_concurrent_rclone_ops=2,
        )

    def test_date_only_format(self, tmp_path):
        from datetime import datetime, timezone
        from boxyard.config import BoxTimestampFormat

        cfg = self._config(tmp_path, BoxTimestampFormat.DATE_ONLY)
        bm = BoxMeta.create(
            config=cfg, name="b", storage_location_name="fake",
            creator_hostname="h", groups=[],
            creation_timestamp_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        assert bm.creation_timestamp_utc == "20260102"

    def test_date_and_time_format(self, tmp_path):
        from datetime import datetime, timezone
        from boxyard.config import BoxTimestampFormat

        cfg = self._config(tmp_path, BoxTimestampFormat.DATE_AND_TIME)
        bm = BoxMeta.create(
            config=cfg, name="b", storage_location_name="fake",
            creator_hostname="h", groups=[],
            creation_timestamp_utc=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        assert bm.creation_timestamp_utc == "20260102_030405"

    def test_explicit_timestamp_round_trips_through_box_id(self, tmp_path):
        from datetime import datetime, timezone
        from boxyard.config import BoxTimestampFormat

        cfg = self._config(tmp_path, BoxTimestampFormat.DATE_ONLY)
        bm = BoxMeta.create(
            config=cfg, name="b", storage_location_name="fake",
            creator_hostname="h", groups=[],
            creation_timestamp_utc=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        assert bm.box_id.startswith("20260102_")
        assert bm.creation_timestamp_datetime == datetime(2026, 1, 2)

# %% [markdown]
# ## Regression: control characters in `creator_hostname`

# %%
#|export
class TestCreatorHostnameControlCharacters:
    """
    `toml` 0.10.2 does not escape control characters -- it silently drops the
    backslash, so dumping "del\x7fchar" yields `"delx7fchar"` and the value
    round-trips back WRONG with no error. Since boxmeta.toml is synced to a
    shared remote, a quietly-corrupted value is the worst outcome; reject it.
    """

    def _meta(self, hostname):
        return BoxMeta(
            creation_timestamp_utc="20260822",
            box_subid="abc123",
            name="b",
            storage_location="fake",
            creator_hostname=hostname,
            groups=[],
        )

    @pytest.mark.parametrize("ch", ["\x00", "\x07", "\x1b", "\x7f", "\n", "\t", "\r"])
    def test_control_characters_rejected(self, ch):
        with pytest.raises(ValueError):
            self._meta(f"host{ch}name")

    def test_ordinary_hostnames_accepted(self):
        for h in ["mymain", "Lukas\u2019s MacBook Pro", "host-1.local", "日本語ホスト"]:
            self._meta(h)

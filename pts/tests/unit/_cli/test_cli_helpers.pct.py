# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for CLI Helper Functions
#
# Tests for CLI utility functions like subsequence matching, name matching modes,
# and box filtering.

# %%
#|default_exp unit._cli.test_cli_helpers

# %%
#|export
import pytest
from unittest.mock import MagicMock
from boxyard._cli.main import _is_subsequence_match, NameMatchMode, _get_filtered_box_metas


# ============================================================================
# Tests for _is_subsequence_match
# ============================================================================

# %%
#|export
class TestIsSubsequenceMatch:
    """Tests for the _is_subsequence_match helper function."""

    def test_exact_match(self):
        """Exact string matches."""
        assert _is_subsequence_match("hello", "hello") == True

    def test_subsequence_at_start(self):
        """Subsequence at the start of the string."""
        assert _is_subsequence_match("hel", "hello") == True

    def test_subsequence_at_end(self):
        """Subsequence at the end of the string."""
        assert _is_subsequence_match("llo", "hello") == True

    def test_subsequence_scattered(self):
        """Subsequence with characters scattered throughout."""
        assert _is_subsequence_match("hlo", "hello") == True

    def test_subsequence_with_gaps(self):
        """Subsequence with larger gaps between characters."""
        assert _is_subsequence_match("ad", "abcd") == True

    def test_lukas_in_lukastk(self):
        """Real example: lukas in lukastk."""
        assert _is_subsequence_match("lukas", "lukastk") == True

    def test_lukas_in_sentence(self):
        """Subsequence found in a longer sentence."""
        assert _is_subsequence_match("lukas", "I am lukastk") == True

    def test_non_subsequence(self):
        """Characters not in correct order fail."""
        assert _is_subsequence_match("acbd", "abcd") == False

    def test_term_longer_than_name(self):
        """Term longer than name fails."""
        assert _is_subsequence_match("hello world", "hello") == False

    def test_empty_term(self):
        """Empty term matches any string."""
        assert _is_subsequence_match("", "anything") == True

    def test_empty_name(self):
        """Non-empty term doesn't match empty name."""
        assert _is_subsequence_match("a", "") == False

    def test_both_empty(self):
        """Empty term matches empty name."""
        assert _is_subsequence_match("", "") == True

    def test_single_char_match(self):
        """Single character match."""
        assert _is_subsequence_match("a", "abc") == True

    def test_single_char_no_match(self):
        """Single character no match."""
        assert _is_subsequence_match("z", "abc") == False

    def test_repeated_chars_in_term(self):
        """Repeated characters in term."""
        assert _is_subsequence_match("aa", "abab") == True
        assert _is_subsequence_match("aaa", "abab") == False

    def test_case_sensitive(self):
        """Match is case sensitive."""
        assert _is_subsequence_match("ABC", "abc") == False
        assert _is_subsequence_match("abc", "ABC") == False
        assert _is_subsequence_match("ABC", "ABC") == True

    def test_special_characters(self):
        """Special characters are matched literally."""
        assert _is_subsequence_match("a-b", "a-b-c") == True
        assert _is_subsequence_match("a_b", "a_b_c") == True

    def test_numbers(self):
        """Numbers are matched correctly."""
        assert _is_subsequence_match("123", "a1b2c3") == True
        assert _is_subsequence_match("321", "a1b2c3") == False

    def test_unicode_characters(self):
        """Unicode characters are handled."""
        assert _is_subsequence_match("日本", "日本語") == True
        assert _is_subsequence_match("こに", "こんにちは") == True


# ============================================================================
# Tests for NameMatchMode enum
# ============================================================================

# %%
#|export
class TestNameMatchMode:
    """Tests for the NameMatchMode enum."""

    def test_exact_value(self):
        """EXACT has correct value."""
        assert NameMatchMode.EXACT.value == "exact"

    def test_contains_value(self):
        """CONTAINS has correct value."""
        assert NameMatchMode.CONTAINS.value == "contains"

    def test_subsequence_value(self):
        """SUBSEQUENCE has correct value."""
        assert NameMatchMode.SUBSEQUENCE.value == "subsequence"

    def test_mode_count(self):
        """There are exactly 3 name match modes."""
        assert len(NameMatchMode) == 3

    def test_modes_from_string(self):
        """Modes can be created from string values."""
        assert NameMatchMode("exact") == NameMatchMode.EXACT
        assert NameMatchMode("contains") == NameMatchMode.CONTAINS
        assert NameMatchMode("subsequence") == NameMatchMode.SUBSEQUENCE

    def test_invalid_mode_raises(self):
        """Invalid mode string raises ValueError."""
        with pytest.raises(ValueError):
            NameMatchMode("invalid")


# ============================================================================
# Tests for _get_filtered_box_metas
# ============================================================================

# %%
#|export
def _create_mock_box_meta(name: str, groups: list[str]) -> MagicMock:
    """Create a mock BoxMeta with name and groups."""
    mock = MagicMock()
    mock.name = name
    mock.groups = groups
    return mock

# %%
#|export
class TestGetFilteredBoxMetas:
    """Tests for the _get_filtered_box_metas function."""

    def test_no_filters_returns_all(self):
        """With no filters, all boxes are returned."""
        boxes = [
            _create_mock_box_meta("box1", ["backend"]),
            _create_mock_box_meta("box2", ["frontend"]),
            _create_mock_box_meta("box3", ["backend", "api"]),
        ]

        result = _get_filtered_box_metas(boxes, None, None, None)
        assert len(result) == 3

    def test_include_single_group(self):
        """Include filter with single group."""
        boxes = [
            _create_mock_box_meta("box1", ["backend"]),
            _create_mock_box_meta("box2", ["frontend"]),
            _create_mock_box_meta("box3", ["backend", "api"]),
        ]

        result = _get_filtered_box_metas(boxes, ["backend"], None, None)
        assert len(result) == 2
        assert all("backend" in r.groups for r in result)

    def test_include_multiple_groups(self):
        """Include filter with multiple groups (OR logic)."""
        boxes = [
            _create_mock_box_meta("box1", ["backend"]),
            _create_mock_box_meta("box2", ["frontend"]),
            _create_mock_box_meta("box3", ["api"]),
            _create_mock_box_meta("box4", ["other"]),
        ]

        result = _get_filtered_box_metas(boxes, ["backend", "frontend"], None, None)
        assert len(result) == 2

    def test_exclude_single_group(self):
        """Exclude filter with single group."""
        boxes = [
            _create_mock_box_meta("box1", ["backend"]),
            _create_mock_box_meta("box2", ["frontend"]),
            _create_mock_box_meta("box3", ["backend", "deprecated"]),
        ]

        result = _get_filtered_box_metas(boxes, None, ["deprecated"], None)
        assert len(result) == 2
        assert all("deprecated" not in r.groups for r in result)

    def test_exclude_multiple_groups(self):
        """Exclude filter with multiple groups."""
        boxes = [
            _create_mock_box_meta("box1", ["backend"]),
            _create_mock_box_meta("box2", ["frontend", "deprecated"]),
            _create_mock_box_meta("box3", ["api", "legacy"]),
            _create_mock_box_meta("box4", ["other"]),
        ]

        result = _get_filtered_box_metas(boxes, None, ["deprecated", "legacy"], None)
        assert len(result) == 2

    def test_include_and_exclude_combined(self):
        """Include and exclude filters work together."""
        boxes = [
            _create_mock_box_meta("box1", ["backend"]),
            _create_mock_box_meta("box2", ["backend", "deprecated"]),
            _create_mock_box_meta("box3", ["frontend"]),
        ]

        result = _get_filtered_box_metas(boxes, ["backend"], ["deprecated"], None)
        assert len(result) == 1
        assert result[0].name == "box1"

    def test_group_filter_simple(self):
        """Group filter with simple expression."""
        boxes = [
            _create_mock_box_meta("box1", ["backend"]),
            _create_mock_box_meta("box2", ["frontend"]),
            _create_mock_box_meta("box3", ["backend", "api"]),
        ]

        result = _get_filtered_box_metas(boxes, None, None, "backend")
        assert len(result) == 2
        assert all("backend" in r.groups for r in result)

    def test_group_filter_and_expression(self):
        """Group filter with AND expression."""
        boxes = [
            _create_mock_box_meta("box1", ["backend"]),
            _create_mock_box_meta("box2", ["backend", "api"]),
            _create_mock_box_meta("box3", ["frontend", "api"]),
        ]

        result = _get_filtered_box_metas(boxes, None, None, "backend AND api")
        assert len(result) == 1
        assert result[0].name == "box2"

    def test_group_filter_or_expression(self):
        """Group filter with OR expression."""
        boxes = [
            _create_mock_box_meta("box1", ["backend"]),
            _create_mock_box_meta("box2", ["frontend"]),
            _create_mock_box_meta("box3", ["other"]),
        ]

        result = _get_filtered_box_metas(boxes, None, None, "backend OR frontend")
        assert len(result) == 2

    def test_group_filter_not_expression(self):
        """Group filter with NOT expression."""
        boxes = [
            _create_mock_box_meta("box1", ["backend"]),
            _create_mock_box_meta("box2", ["backend", "deprecated"]),
            _create_mock_box_meta("box3", ["frontend"]),
        ]

        result = _get_filtered_box_metas(boxes, None, None, "backend AND NOT deprecated")
        assert len(result) == 1
        assert result[0].name == "box1"

    def test_group_filter_complex_expression(self):
        """Group filter with complex expression."""
        boxes = [
            _create_mock_box_meta("box1", ["backend", "prod"]),
            _create_mock_box_meta("box2", ["backend", "staging"]),
            _create_mock_box_meta("box3", ["frontend", "prod"]),
            _create_mock_box_meta("box4", ["backend", "prod", "deprecated"]),
        ]

        result = _get_filtered_box_metas(
            boxes, None, None, "(backend OR frontend) AND prod AND NOT deprecated"
        )
        assert len(result) == 2
        names = [r.name for r in result]
        assert "box1" in names
        assert "box3" in names

    def test_all_filters_combined(self):
        """Include, exclude, and group filter all together."""
        boxes = [
            _create_mock_box_meta("box1", ["backend", "api"]),
            _create_mock_box_meta("box2", ["backend", "api", "deprecated"]),
            _create_mock_box_meta("box3", ["frontend", "api"]),
            _create_mock_box_meta("box4", ["other"]),
        ]

        # Include api, exclude deprecated, then filter for backend
        result = _get_filtered_box_metas(boxes, ["api"], ["deprecated"], "backend")
        assert len(result) == 1
        assert result[0].name == "box1"

    def test_empty_box_list(self):
        """Empty box list returns empty result."""
        result = _get_filtered_box_metas([], ["backend"], None, None)
        assert len(result) == 0

    def test_no_matches_returns_empty(self):
        """No matching boxes returns empty list."""
        boxes = [
            _create_mock_box_meta("box1", ["frontend"]),
            _create_mock_box_meta("box2", ["other"]),
        ]

        result = _get_filtered_box_metas(boxes, ["backend"], None, None)
        assert len(result) == 0

    def test_box_with_no_groups(self):
        """Box with no groups is handled correctly."""
        boxes = [
            _create_mock_box_meta("box1", []),
            _create_mock_box_meta("box2", ["backend"]),
        ]

        # Include filter excludes box with no groups
        result = _get_filtered_box_metas(boxes, ["backend"], None, None)
        assert len(result) == 1

        # Exclude filter keeps box with no groups
        result = _get_filtered_box_metas(boxes, None, ["backend"], None)
        assert len(result) == 1
        assert result[0].name == "box1"

    def test_group_filter_with_empty_groups(self):
        """Group filter handles boxes with empty groups."""
        boxes = [
            _create_mock_box_meta("box1", []),
            _create_mock_box_meta("box2", ["backend"]),
        ]

        result = _get_filtered_box_metas(boxes, None, None, "NOT backend")
        assert len(result) == 1
        assert result[0].name == "box1"


# ============================================================================
# Tests for timestamp parsing (from cli_new)
# ============================================================================

# %%
#|export
from datetime import datetime
from boxyard import const


class TestTimestampParsing:
    """Tests for timestamp parsing in CLI commands."""

    def test_full_timestamp_format(self):
        """Full timestamp format YYYYMMDD_HHMMSS is parsed correctly."""
        timestamp_str = "20251116_105532"
        result = datetime.strptime(timestamp_str, const.BOX_TIMESTAMP_FORMAT)
        assert result.year == 2025
        assert result.month == 11
        assert result.day == 16
        assert result.hour == 10
        assert result.minute == 55
        assert result.second == 32

    def test_date_only_format(self):
        """Date-only format YYYYMMDD is parsed correctly."""
        timestamp_str = "20251116"
        result = datetime.strptime(timestamp_str, const.BOX_TIMESTAMP_FORMAT_DATE_ONLY)
        assert result.year == 2025
        assert result.month == 11
        assert result.day == 16
        assert result.hour == 0
        assert result.minute == 0
        assert result.second == 0

    def test_invalid_timestamp_raises(self):
        """Invalid timestamp format raises ValueError."""
        with pytest.raises(ValueError):
            datetime.strptime("2025-11-16", const.BOX_TIMESTAMP_FORMAT)

    def test_invalid_date_only_raises(self):
        """Invalid date-only format raises ValueError."""
        with pytest.raises(ValueError):
            datetime.strptime("2025-11-16", const.BOX_TIMESTAMP_FORMAT_DATE_ONLY)

    def test_partial_timestamp_raises(self):
        """Partial timestamp raises ValueError."""
        with pytest.raises(ValueError):
            datetime.strptime("20251116_10", const.BOX_TIMESTAMP_FORMAT)


# ============================================================================
# Tests for box path inference helper
# ============================================================================

# %%
#|export
from unittest.mock import patch


class TestBoxPathInference:
    """Tests for get_box_index_name_from_sub_path function.

    Uses real temp directories since the function calls Path.resolve().
    """

    def test_path_within_box_data(self, tmp_path):
        """Path within box data directory returns index name."""
        from boxyard._utils import get_box_index_name_from_sub_path

        # Create real directory structure
        boxes_path = tmp_path / "boxes"
        box_dir = boxes_path / "20251116_123456_abc12__mybox" / "subdir"
        box_dir.mkdir(parents=True)

        mock_config = MagicMock()
        mock_config.user_boxes_path = boxes_path

        result = get_box_index_name_from_sub_path(mock_config, box_dir)
        assert result == "20251116_123456_abc12__mybox"

    def test_path_at_box_root(self, tmp_path):
        """Path at box root returns index name."""
        from boxyard._utils import get_box_index_name_from_sub_path

        boxes_path = tmp_path / "boxes"
        box_dir = boxes_path / "20251116_123456_abc12__mybox"
        box_dir.mkdir(parents=True)

        mock_config = MagicMock()
        mock_config.user_boxes_path = boxes_path

        result = get_box_index_name_from_sub_path(mock_config, box_dir)
        assert result == "20251116_123456_abc12__mybox"

    def test_path_outside_boxes(self, tmp_path):
        """Path outside boxes directory returns None."""
        from boxyard._utils import get_box_index_name_from_sub_path

        boxes_path = tmp_path / "boxes"
        boxes_path.mkdir(parents=True)
        other_dir = tmp_path / "other" / "directory"
        other_dir.mkdir(parents=True)

        mock_config = MagicMock()
        mock_config.user_boxes_path = boxes_path

        result = get_box_index_name_from_sub_path(mock_config, other_dir)
        assert result is None

    def test_path_at_boxes_root(self, tmp_path):
        """Path at boxes root (not inside a box) returns None."""
        from boxyard._utils import get_box_index_name_from_sub_path

        boxes_path = tmp_path / "boxes"
        boxes_path.mkdir(parents=True)

        mock_config = MagicMock()
        mock_config.user_boxes_path = boxes_path

        result = get_box_index_name_from_sub_path(mock_config, boxes_path)
        assert result is None

    def test_deeply_nested_path(self, tmp_path):
        """Deeply nested path still returns correct index name."""
        from boxyard._utils import get_box_index_name_from_sub_path

        boxes_path = tmp_path / "boxes"
        deep_dir = boxes_path / "20251116_123456_abc12__mybox" / "a" / "b" / "c" / "d" / "e"
        deep_dir.mkdir(parents=True)

        mock_config = MagicMock()
        mock_config.user_boxes_path = boxes_path

        result = get_box_index_name_from_sub_path(mock_config, deep_dir)
        assert result == "20251116_123456_abc12__mybox"

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for SyncRecord, SyncCondition, and SyncStatus

# %%
#|default_exp unit.models.test_sync_record

# %%
#|export
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
from pydantic import ValidationError
from ulid import ULID

from boxyard._models import SyncRecord, SyncCondition, SyncStatus


# ============================================================================
# Tests for SyncRecord construction
# ============================================================================

# %%
#|export
class TestSyncRecordConstruction:
    """Tests for SyncRecord basic construction."""

    def test_construction_with_defaults(self):
        """SyncRecord generates ULID by default."""
        record = SyncRecord(
            sync_complete=True,
            syncer_hostname="testhost",
        )

        assert record.ulid is not None
        assert isinstance(record.ulid, ULID)
        assert record.sync_complete is True
        assert record.syncer_hostname == "testhost"

    def test_construction_with_explicit_ulid(self):
        """SyncRecord accepts explicit ULID."""
        explicit_ulid = ULID()
        record = SyncRecord(
            ulid=explicit_ulid,
            sync_complete=False,
            syncer_hostname="testhost",
        )

        assert record.ulid == explicit_ulid

    def test_timestamp_auto_populated(self):
        """timestamp is auto-populated from ULID."""
        record = SyncRecord(
            sync_complete=True,
            syncer_hostname="testhost",
        )

        assert record.timestamp is not None
        assert record.timestamp == record.ulid.datetime

    def test_timestamp_must_match_ulid(self):
        """timestamp must match ULID datetime if explicitly provided."""
        ulid = ULID()
        correct_timestamp = ulid.datetime

        # This should work
        record = SyncRecord(
            ulid=ulid,
            timestamp=correct_timestamp,
            sync_complete=True,
            syncer_hostname="testhost",
        )
        assert record.timestamp == correct_timestamp

    def test_timestamp_mismatch_raises_error(self):
        """Mismatched timestamp raises ValidationError."""
        ulid = ULID()
        wrong_timestamp = ulid.datetime + timedelta(hours=1)

        with pytest.raises(ValidationError, match="timestamp"):
            SyncRecord(
                ulid=ulid,
                timestamp=wrong_timestamp,
                sync_complete=True,
                syncer_hostname="testhost",
            )


# ============================================================================
# Tests for SyncRecord.create factory method
# ============================================================================

# %%
#|export
class TestSyncRecordCreate:
    """Tests for the SyncRecord.create factory method."""

    def test_create_generates_new_record(self):
        """create() generates a new SyncRecord with ULID."""
        # get_hostname is imported via from ._utils import get_hostname
        # which re-exports from base via from .base import *
        with patch("boxyard._utils.get_hostname", return_value="autohost"):
            record = SyncRecord.create(sync_complete=True)

        assert record.ulid is not None
        assert record.sync_complete is True
        assert record.syncer_hostname == "autohost"

    def test_create_with_explicit_hostname(self):
        """create() accepts explicit hostname."""
        record = SyncRecord.create(
            sync_complete=False,
            syncer_hostname="explicit-host",
        )

        assert record.syncer_hostname == "explicit-host"

    def test_create_incomplete_sync(self):
        """create() can create incomplete sync records."""
        record = SyncRecord.create(
            sync_complete=False,
            syncer_hostname="testhost",
        )

        assert record.sync_complete is False


# ============================================================================
# Tests for SyncRecord ULID properties
# ============================================================================

# %%
#|export
class TestSyncRecordULID:
    """Tests for ULID-related behavior."""

    def test_ulids_are_sortable_by_time(self):
        """ULIDs generated at different times are sortable."""
        import time

        records = []
        for _ in range(3):
            records.append(SyncRecord(sync_complete=True, syncer_hostname="host"))
            time.sleep(0.002)  # Small delay to ensure different timestamps

        # ULIDs should be in ascending order
        for i in range(len(records) - 1):
            assert records[i].ulid < records[i + 1].ulid

    def test_ulid_contains_timestamp(self):
        """ULID datetime is reasonably close to now."""
        before = datetime.now(timezone.utc)
        record = SyncRecord(sync_complete=True, syncer_hostname="host")
        after = datetime.now(timezone.utc)

        # ULID has millisecond precision, so we allow 1ms tolerance
        # by comparing at millisecond level
        tolerance = timedelta(milliseconds=2)
        assert before - tolerance <= record.ulid.datetime <= after + tolerance

    def test_different_records_have_different_ulids(self):
        """Each record gets a unique ULID."""
        records = [SyncRecord(sync_complete=True, syncer_hostname="host") for _ in range(100)]
        ulids = [r.ulid for r in records]

        assert len(set(str(u) for u in ulids)) == 100  # All unique


# ============================================================================
# Tests for SyncRecord serialization
# ============================================================================

# %%
#|export
class TestSyncRecordSerialization:
    """Tests for SyncRecord JSON serialization."""

    def test_model_dump_json(self):
        """SyncRecord can be serialized to JSON."""
        record = SyncRecord(
            sync_complete=True,
            syncer_hostname="testhost",
        )

        json_str = record.model_dump_json()
        assert "sync_complete" in json_str
        assert "syncer_hostname" in json_str
        assert "testhost" in json_str

    def test_model_validate_json(self):
        """SyncRecord can be deserialized from JSON."""
        record = SyncRecord(
            sync_complete=True,
            syncer_hostname="testhost",
        )

        json_str = record.model_dump_json()
        restored = SyncRecord.model_validate_json(json_str)

        assert restored.ulid == record.ulid
        assert restored.sync_complete == record.sync_complete
        assert restored.syncer_hostname == record.syncer_hostname

    def test_roundtrip_preserves_ulid(self):
        """JSON roundtrip preserves the exact ULID."""
        original = SyncRecord(
            sync_complete=False,
            syncer_hostname="host123",
        )

        json_str = original.model_dump_json()
        restored = SyncRecord.model_validate_json(json_str)

        assert str(restored.ulid) == str(original.ulid)
        assert restored.timestamp == original.timestamp


# ============================================================================
# Tests for SyncCondition enum
# ============================================================================

# %%
#|export
class TestSyncCondition:
    """Tests for the SyncCondition enum."""

    def test_all_conditions_have_values(self):
        """All SyncCondition members have string values."""
        assert SyncCondition.SYNCED.value == "synced"
        assert SyncCondition.SYNC_TO_REMOTE_INCOMPLETE.value == "sync_to_remote_incomplete"
        assert SyncCondition.SYNC_FROM_REMOTE_INCOMPLETE.value == "sync_from_remote_incomplete"
        assert SyncCondition.CONFLICT.value == "conflict"
        assert SyncCondition.NEEDS_PUSH.value == "needs_push"
        assert SyncCondition.NEEDS_PULL.value == "needs_pull"
        assert SyncCondition.EXCLUDED.value == "excluded"
        assert SyncCondition.ERROR.value == "error"

    def test_condition_count(self):
        """There are exactly 9 sync conditions."""
        assert len(SyncCondition) == 9

    def test_conditions_are_unique(self):
        """All condition values are unique."""
        values = [c.value for c in SyncCondition]
        assert len(values) == len(set(values))

    def test_conditions_are_iterable(self):
        """SyncCondition can be iterated."""
        conditions = list(SyncCondition)
        assert SyncCondition.SYNCED in conditions
        assert SyncCondition.CONFLICT in conditions
        assert SyncCondition.ERROR in conditions


# ============================================================================
# Tests for SyncStatus NamedTuple
# ============================================================================

# %%
#|export
class TestSyncStatus:
    """Tests for the SyncStatus NamedTuple."""

    def test_sync_status_construction(self):
        """SyncStatus can be constructed with all fields."""
        record = SyncRecord(sync_complete=True, syncer_hostname="host")

        status = SyncStatus(
            sync_condition=SyncCondition.SYNCED,
            local_path_exists=True,
            remote_path_exists=True,
            local_sync_record=record,
            remote_sync_record=record,
            is_dir=True,
        )

        assert status.sync_condition == SyncCondition.SYNCED
        assert status.local_path_exists is True
        assert status.remote_path_exists is True
        assert status.local_sync_record == record
        assert status.remote_sync_record == record
        assert status.is_dir is True
        assert status.error_message is None

    def test_sync_status_with_error(self):
        """SyncStatus can include an error message."""
        status = SyncStatus(
            sync_condition=SyncCondition.ERROR,
            local_path_exists=True,
            remote_path_exists=False,
            local_sync_record=None,
            remote_sync_record=None,
            is_dir=True,
            error_message="Remote sync record missing",
        )

        assert status.sync_condition == SyncCondition.ERROR
        assert status.error_message == "Remote sync record missing"

    def test_sync_status_with_none_records(self):
        """SyncStatus allows None for sync records."""
        status = SyncStatus(
            sync_condition=SyncCondition.NEEDS_PUSH,
            local_path_exists=True,
            remote_path_exists=False,
            local_sync_record=None,
            remote_sync_record=None,
            is_dir=True,
        )

        assert status.local_sync_record is None
        assert status.remote_sync_record is None

    def test_sync_status_is_namedtuple(self):
        """SyncStatus is a NamedTuple with correct field names."""
        assert hasattr(SyncStatus, "_fields")
        fields = SyncStatus._fields

        assert "sync_condition" in fields
        assert "local_path_exists" in fields
        assert "remote_path_exists" in fields
        assert "local_sync_record" in fields
        assert "remote_sync_record" in fields
        assert "is_dir" in fields
        assert "error_message" in fields

    def test_sync_status_field_access_by_index(self):
        """SyncStatus fields can be accessed by index."""
        record = SyncRecord(sync_complete=True, syncer_hostname="host")

        status = SyncStatus(
            sync_condition=SyncCondition.SYNCED,
            local_path_exists=True,
            remote_path_exists=True,
            local_sync_record=record,
            remote_sync_record=record,
            is_dir=False,
        )

        # Access by index
        assert status[0] == SyncCondition.SYNCED  # sync_condition
        assert status[1] is True  # local_path_exists
        assert status[2] is True  # remote_path_exists


# ============================================================================
# Tests for different sync scenarios
# ============================================================================

# %%
#|export
class TestSyncScenarios:
    """Tests demonstrating different sync condition scenarios."""

    def test_synced_scenario(self):
        """SYNCED: local and remote match."""
        record = SyncRecord(sync_complete=True, syncer_hostname="host")

        status = SyncStatus(
            sync_condition=SyncCondition.SYNCED,
            local_path_exists=True,
            remote_path_exists=True,
            local_sync_record=record,
            remote_sync_record=record,  # Same record on both sides
            is_dir=True,
        )

        assert status.sync_condition == SyncCondition.SYNCED

    def test_needs_push_scenario(self):
        """NEEDS_PUSH: local has changes, remote doesn't exist."""
        status = SyncStatus(
            sync_condition=SyncCondition.NEEDS_PUSH,
            local_path_exists=True,
            remote_path_exists=False,
            local_sync_record=None,
            remote_sync_record=None,
            is_dir=True,
        )

        assert status.sync_condition == SyncCondition.NEEDS_PUSH

    def test_needs_pull_scenario(self):
        """NEEDS_PULL: remote has newer changes."""
        local_record = SyncRecord(sync_complete=True, syncer_hostname="host1")

        # Simulate a newer remote record
        import time
        time.sleep(0.002)
        remote_record = SyncRecord(sync_complete=True, syncer_hostname="host2")

        status = SyncStatus(
            sync_condition=SyncCondition.NEEDS_PULL,
            local_path_exists=True,
            remote_path_exists=True,
            local_sync_record=local_record,
            remote_sync_record=remote_record,
            is_dir=True,
        )

        assert status.sync_condition == SyncCondition.NEEDS_PULL
        assert remote_record.ulid > local_record.ulid

    def test_conflict_scenario(self):
        """CONFLICT: both sides have different changes."""
        local_record = SyncRecord(sync_complete=True, syncer_hostname="host1")
        remote_record = SyncRecord(sync_complete=True, syncer_hostname="host2")

        status = SyncStatus(
            sync_condition=SyncCondition.CONFLICT,
            local_path_exists=True,
            remote_path_exists=True,
            local_sync_record=local_record,
            remote_sync_record=remote_record,
            is_dir=True,
        )

        assert status.sync_condition == SyncCondition.CONFLICT

    def test_excluded_scenario(self):
        """EXCLUDED: exists remotely but not locally."""
        remote_record = SyncRecord(sync_complete=True, syncer_hostname="host")

        status = SyncStatus(
            sync_condition=SyncCondition.EXCLUDED,
            local_path_exists=False,
            remote_path_exists=True,
            local_sync_record=None,
            remote_sync_record=remote_record,
            is_dir=True,
        )

        assert status.sync_condition == SyncCondition.EXCLUDED

    def test_sync_to_remote_incomplete_scenario(self):
        """SYNC_TO_REMOTE_INCOMPLETE: a push is in progress or was interrupted."""
        incomplete_record = SyncRecord(
            sync_complete=False,  # Not complete
            syncer_hostname="host",
        )

        status = SyncStatus(
            sync_condition=SyncCondition.SYNC_TO_REMOTE_INCOMPLETE,
            local_path_exists=True,
            remote_path_exists=True,
            local_sync_record=incomplete_record,
            remote_sync_record=incomplete_record,
            is_dir=True,
        )

        assert status.sync_condition == SyncCondition.SYNC_TO_REMOTE_INCOMPLETE
        assert status.local_sync_record.sync_complete is False

    def test_sync_from_remote_incomplete_scenario(self):
        """SYNC_FROM_REMOTE_INCOMPLETE: a pull is in progress or was interrupted."""
        incomplete_record = SyncRecord(
            sync_complete=False,  # Not complete
            syncer_hostname="host",
        )
        complete_record = SyncRecord(
            sync_complete=True,
            syncer_hostname="host",
        )

        status = SyncStatus(
            sync_condition=SyncCondition.SYNC_FROM_REMOTE_INCOMPLETE,
            local_path_exists=True,
            remote_path_exists=True,
            local_sync_record=incomplete_record,
            remote_sync_record=complete_record,
            is_dir=True,
        )

        assert status.sync_condition == SyncCondition.SYNC_FROM_REMOTE_INCOMPLETE
        assert status.local_sync_record.sync_complete is False
        assert status.remote_sync_record.sync_complete is True


# ============================================================================
# Tests for edge cases
# ============================================================================

# %%
#|export
class TestSyncRecordEdgeCases:
    """Tests for edge cases in SyncRecord."""

    def test_empty_hostname(self):
        """Empty hostname is allowed (though not recommended)."""
        record = SyncRecord(
            sync_complete=True,
            syncer_hostname="",
        )
        assert record.syncer_hostname == ""

    def test_special_characters_in_hostname(self):
        """Hostnames with special characters are allowed."""
        record = SyncRecord(
            sync_complete=True,
            syncer_hostname="my-host.local",
        )
        assert record.syncer_hostname == "my-host.local"

    def test_unicode_hostname(self):
        """Unicode in hostname is allowed."""
        record = SyncRecord(
            sync_complete=True,
            syncer_hostname="hôst-名前",
        )
        assert record.syncer_hostname == "hôst-名前"

    def test_very_long_hostname(self):
        """Very long hostnames are allowed."""
        long_hostname = "a" * 1000
        record = SyncRecord(
            sync_complete=True,
            syncer_hostname=long_hostname,
        )
        assert len(record.syncer_hostname) == 1000

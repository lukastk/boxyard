# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # A Caller-Supplied Timestamp Must Be Part of the Collision Check
#
# `new_box` used to ask `generate_unique_box_id` for an id, let it check
# `<now>_<subid>` against the existing ids, and only THEN overwrite the
# timestamp half with the caller's `--creation-timestamp-utc` — so the id that
# was written was never the id that was checked. Every collision guarantee the
# function offers was void for that path.
#
# The fix threads the resolved timestamp INTO the generator, so the loop
# retries the subid against the id that will actually be used.

# %%
#|default_exp unit.models.test_generate_unique_box_id

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
from datetime import datetime, timezone

import pytest

import boxyard._models as _models
from boxyard._models import format_creation_timestamp, generate_unique_box_id
from boxyard.config import BoxTimestampFormat, Config, StorageConfig, StorageType


def _config(tmp_path, fmt=BoxTimestampFormat.DATE_ONLY) -> Config:
    """A config carrying only what id generation reads."""
    return Config(
        config_path=tmp_path / "config.toml",
        default_storage_location="s",
        boxyard_data_path=tmp_path / "data",
        box_timestamp_format=fmt,
        user_boxes_path=tmp_path / "boxes",
        user_box_groups_path=tmp_path / "groups",
        storage_locations={
            "s": StorageConfig(
                storage_type=StorageType.LOCAL, store_path=tmp_path / "store"
            )
        },
        box_groups={},
        virtual_box_groups={},
        default_box_groups=[],
        box_subid_character_set="ab",
        box_subid_length=1,
        max_concurrent_rclone_ops=1,
    )

# %% [markdown]
# ## A fixed timestamp is used verbatim, and its subid avoids the taken ids

# %%
#|export
def test_fixed_timestamp_collision_is_avoided(tmp_path):
    config = _config(tmp_path)
    # The character set is "ab" and the length 1, so there are exactly two
    # possible ids for a given timestamp. Taking one leaves the generator no
    # choice but to return the other — which is only true if it is checking
    # against the timestamp it was handed.
    # Repeated because the subid is random: a generator that checks against
    # the WRONG timestamp still returns "b" half the time, and a single draw
    # would pass by luck.
    for _ in range(20):
        # TESTREF: test_new_box_fixed_timestamp_is_collision_checked
        box_id, subid = generate_unique_box_id(
            config, existing_ids={"20240102_a"}, creation_timestamp="20240102"
        )
        assert box_id == "20240102"
        assert subid == "b"


def test_fixed_timestamp_exhausted_raises(tmp_path):
    config = _config(tmp_path)
    # Both ids taken: there is no unique id to be had, and the generator must
    # say so rather than hand back a duplicate.
    with pytest.raises(RuntimeError):
        generate_unique_box_id(
            config,
            existing_ids={"20240102_a", "20240102_b"},
            creation_timestamp="20240102",
            max_attempts=20,
        )

# %% [markdown]
# ## Without an override the timestamp is still "now"

# %%
#|export
def test_default_timestamp_is_now(tmp_path):
    config = _config(tmp_path)
    expected = format_creation_timestamp(config, datetime.now(timezone.utc))
    box_id, _ = generate_unique_box_id(config, existing_ids=set())
    assert box_id == expected

# %% [markdown]
# ## `format_creation_timestamp` covers both formats, and refuses a third

# %%
#|export
def test_format_creation_timestamp(tmp_path):
    dt = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert format_creation_timestamp(_config(tmp_path, BoxTimestampFormat.DATE_ONLY), dt) == "20240102"
    assert (
        format_creation_timestamp(
            _config(tmp_path, BoxTimestampFormat.DATE_AND_TIME), dt
        )
        == "20240102_030405"
    )


def test_format_creation_timestamp_rejects_unknown(tmp_path):
    config = _config(tmp_path)
    # `model_construct` skips validation, which is the only way to build the
    # invalid state this branch guards against.
    broken = Config.model_construct(
        **{**config.model_dump(), "box_timestamp_format": "sundial"}
    )
    with pytest.raises(Exception):
        format_creation_timestamp(broken, datetime.now(timezone.utc))

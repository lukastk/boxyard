# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for `boxmeta.toml` forward compatibility
#
# Two guarantees, both load-bearing for a fleet running mixed boxyard versions:
#
# 1. **An unowned box's `boxmeta.toml` is byte-identical after a `save()`
#    round-trip.** `write_owner` defaults to `None` and `save()` omits it, so
#    installing this version rewrites nothing — the 583 boxmetas in the live
#    yard stay exactly as they are, and a machine still on an older version
#    sees no change at all.
#
# 2. **A key this version does not know survives a load/save round-trip.** An
#    older machine must not STRIP a newer machine's key: one `boxyard
#    add-to-group` would otherwise silently delete a `write_owner` from the
#    shared boxmeta. And an unknown key must not make the registration
#    unloadable either — a registration that fails to load is SKIPPED by
#    `create_boxyard_meta`, which drops the box out of `boxyard_meta.json`,
#    out of `boxyard list`, out of `~/g` and out of `multi-sync`, silently.

# %%
#|default_exp unit.models.test_boxmeta_forward_compat

# %%
#|export
import pytest
import tomli_w
from pydantic import ValidationError

from boxyard import const
from boxyard._models import BoxMeta, BoxyardMeta
from boxyard.config import (
    BoxTimestampFormat,
    Config,
    StorageConfig,
    StorageType,
)


SL = "fake"
INDEX_NAME = "20260102_abc123__a-box"

# Exactly what a pre-v0.5 boxyard writes for an unowned box: the four
# non-derived fields, in model order. Rendered with the same serializer the
# real `save()` uses, rather than pasted as a literal, so the assertion is
# "the new fields changed nothing" and not "tomli_w still formats arrays the
# way it did when this test was written" -- a formatting change would then
# fail every round-trip test here for a reason that has nothing to do with
# forward compatibility.
#
# (The boxmetas in the live yard predate v0.4.0 and carry the old `toml`
# library's single-line arrays, e.g. `groups = [ "a", "b",]`. v0.4.0 replaced
# that library, so any save already reformats them. That is v0.4.0's doing,
# not this change's, and the two renderings parse identically.)
PRE_V05_FIELDS = {
    "storage_location": SL,
    "creator_hostname": "Lukas’s MacBook Pro",
    "groups": ["ctx/macbook", "worktrees"],
    "parents": [],
}
PRE_V05_BOXMETA = tomli_w.dumps(PRE_V05_FIELDS)


def make_config(tmp_path) -> Config:
    return Config(
        config_path=tmp_path / "config.toml",
        default_storage_location=SL,
        boxyard_data_path=tmp_path / "data",
        box_timestamp_format=BoxTimestampFormat.DATE_ONLY,
        user_boxes_path=tmp_path / "boxes",
        user_box_groups_path=tmp_path / "groups",
        storage_locations={
            SL: StorageConfig(
                storage_type=StorageType.LOCAL, store_path=tmp_path / "store"
            )
        },
        box_groups={},
        virtual_box_groups={},
        default_box_groups=[],
        box_subid_character_set="abcdefghijklmnopqrstuvwxyz0123456789",
        box_subid_length=6,
        max_concurrent_rclone_ops=2,
    )


def write_boxmeta(config: Config, text: str, index_name: str = INDEX_NAME):
    """Put `text` on disk where `BoxMeta.load` will find it, and return the path."""
    path = (
        config.local_store_path / SL / index_name / const.BOX_METAFILE_REL_PATH
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ============================================================================
# An unowned box is written exactly as before
# ============================================================================

# %%
#|export
class TestUnownedBoxmetaIsUnchanged:
    """
    The upgrade must be a no-op on disk. If `save()` wrote `write_owner = ""`
    or a null, every box the machine touched would be rewritten and the
    remote would diverge from the copy on every machine still on 0.4.x.
    """

    def test_round_trip_is_byte_identical(self, tmp_path):
        config = make_config(tmp_path)
        path = write_boxmeta(config, PRE_V05_BOXMETA)

        BoxMeta.load(config, SL, INDEX_NAME).save(config)

        assert path.read_text() == PRE_V05_BOXMETA

    def test_write_owner_key_is_absent_not_null(self, tmp_path):
        config = make_config(tmp_path)
        path = write_boxmeta(config, PRE_V05_BOXMETA)

        BoxMeta.load(config, SL, INDEX_NAME).save(config)

        assert "write_owner" not in path.read_text()

    def test_a_loaded_unowned_box_has_no_owner(self, tmp_path):
        config = make_config(tmp_path)
        write_boxmeta(config, PRE_V05_BOXMETA)

        box_meta = BoxMeta.load(config, SL, INDEX_NAME)

        assert box_meta.write_owner is None
        assert box_meta.unknown_keys == {}


# ============================================================================
# An owned box round-trips, and clearing the owner restores the old format
# ============================================================================

# %%
#|export
class TestWriteOwnerRoundTrip:
    def test_owner_is_written_and_read_back(self, tmp_path):
        config = make_config(tmp_path)
        write_boxmeta(config, PRE_V05_BOXMETA)

        box_meta = BoxMeta.load(config, SL, INDEX_NAME)
        box_meta.write_owner = "mymain"
        box_meta.save(config)

        assert BoxMeta.load(config, SL, INDEX_NAME).write_owner == "mymain"

    def test_clearing_the_owner_restores_the_pre_v05_file(self, tmp_path):
        """`boxyard release` must return the file to a form an old machine reads."""
        config = make_config(tmp_path)
        path = write_boxmeta(config, PRE_V05_BOXMETA)

        box_meta = BoxMeta.load(config, SL, INDEX_NAME)
        box_meta.write_owner = "mymain"
        box_meta.save(config)
        assert path.read_text() != PRE_V05_BOXMETA

        box_meta = BoxMeta.load(config, SL, INDEX_NAME)
        box_meta.write_owner = None
        box_meta.save(config)

        assert path.read_text() == PRE_V05_BOXMETA


# ============================================================================
# `write_owner` is a compared identifier, so it is validated
# ============================================================================

# %%
#|export
class TestWriteOwnerValidation:
    """
    Unlike `creator_hostname` -- a historical label that carries whatever the
    OS reported, spaces and U+2019 included -- `write_owner` decides which
    machine may push. It must survive being compared, printed in an error and
    passed as a command-line argument unchanged.
    """

    def _box_meta(self, **overrides) -> dict:
        return {
            "creation_timestamp_utc": "20260102",
            "box_subid": "abc123",
            "name": "a-box",
            "storage_location": SL,
            "creator_hostname": "host",
            "groups": [],
            **overrides,
        }

    @pytest.mark.parametrize(
        "owner", ["mymain", "macbook", "macstudio", "pocket4", "a", "A_b-9", "x" * 64]
    )
    def test_accepts_canonical_machine_names(self, owner):
        assert BoxMeta(**self._box_meta(write_owner=owner)).write_owner == owner

    @pytest.mark.parametrize(
        "owner",
        [
            "",  # empty
            "x" * 65,  # too long
            "Lukas’s MacBook Pro",  # the macOS pretty name
            "has space",
            "has/slash",
            "has.dot",
            "has\nnewline",
            "has\x00null",
        ],
    )
    def test_rejects_anything_else(self, owner):
        with pytest.raises(ValidationError, match="write_owner"):
            BoxMeta(**self._box_meta(write_owner=owner))


# ============================================================================
# Unknown keys survive, and cannot be confused with known ones
# ============================================================================

# %%
#|export
# The same box as written by a NEWER boxyard: a key this version knows
# (`write_owner`) and two it does not. Key order matches `save()`'s: model
# fields first, then the passthrough.
NEWER_BOXMETA = tomli_w.dumps(
    {
        **PRE_V05_FIELDS,
        "write_owner": "macbook",
        "some_future_key": "a value",
        "another_future_key": [1, 2],
    }
)


class TestUnknownKeyPassthrough:
    def test_an_unknown_key_does_not_break_loading(self, tmp_path):
        """
        The whole point: a newer key must cost a doctor finding, not a box.
        A registration that raises here is skipped by `create_boxyard_meta`,
        and the box then vanishes from `boxyard list`, `~/g` and `multi-sync`.
        """
        config = make_config(tmp_path)
        write_boxmeta(config, NEWER_BOXMETA)

        box_meta = BoxMeta.load(config, SL, INDEX_NAME)

        assert box_meta.unknown_keys == {
            "some_future_key": "a value",
            "another_future_key": [1, 2],
        }

    def test_known_keys_are_not_swallowed_into_the_passthrough(self, tmp_path):
        config = make_config(tmp_path)
        write_boxmeta(config, NEWER_BOXMETA)

        box_meta = BoxMeta.load(config, SL, INDEX_NAME)

        # `write_owner` is known to THIS version, so it is a field, not debris.
        assert box_meta.write_owner == "macbook"
        assert "write_owner" not in box_meta.unknown_keys
        assert box_meta.groups == ["ctx/macbook", "worktrees"]

    def test_round_trip_is_byte_identical(self, tmp_path):
        config = make_config(tmp_path)
        path = write_boxmeta(config, NEWER_BOXMETA)

        BoxMeta.load(config, SL, INDEX_NAME).save(config)

        assert path.read_text() == NEWER_BOXMETA

    def test_add_to_group_does_not_strip_a_newer_machines_keys(self, tmp_path):
        """
        The exact failure this passthrough exists to prevent. `modify_boxmeta`
        (which backs `boxyard add-to-group`) rebuilds the model from
        `model_dump()`, so the passthrough has to survive that too -- not just
        `load` -> `save`.
        """
        config = make_config(tmp_path)
        path = write_boxmeta(config, NEWER_BOXMETA)

        box_meta = BoxMeta.load(config, SL, INDEX_NAME)
        modified = BoxMeta(**{**box_meta.model_dump(), "groups": ["a-new-group"]})
        modified.save(config)

        reloaded = BoxMeta.load(config, SL, INDEX_NAME)
        assert reloaded.groups == ["a-new-group"]
        assert reloaded.write_owner == "macbook"
        assert reloaded.unknown_keys == {
            "some_future_key": "a value",
            "another_future_key": [1, 2],
        }
        assert "some_future_key" in path.read_text()

    def test_survives_the_boxyard_meta_json_cache(self, tmp_path):
        """
        Commands read box metas from `boxyard_meta.json`, not from disk, so a
        passthrough that did not serialize into the cache would be dropped by
        every command that goes through it.
        """
        config = make_config(tmp_path)
        write_boxmeta(config, NEWER_BOXMETA)

        cached = BoxyardMeta(box_metas=[BoxMeta.load(config, SL, INDEX_NAME)])
        restored = BoxyardMeta.model_validate_json(cached.model_dump_json())

        assert restored.box_metas[0].unknown_keys == {
            "some_future_key": "a value",
            "another_future_key": [1, 2],
        }

    def test_the_container_key_itself_is_rejected(self, tmp_path):
        """
        `unknown_keys` in the file is corrupt, not newer: folding it in would
        flatten its contents to top-level keys on the next save.
        """
        config = make_config(tmp_path)
        write_boxmeta(config, PRE_V05_BOXMETA + '[unknown_keys]\nfoo = "bar"\n')

        with pytest.raises(ValueError, match="reserved key"):
            BoxMeta.load(config, SL, INDEX_NAME)

    def test_a_passthrough_shadowing_a_known_field_is_rejected(self):
        """
        Otherwise `save()` would write the same key twice and the file would
        disagree with itself.
        """
        with pytest.raises(ValidationError, match="unknown_keys"):
            BoxMeta(
                creation_timestamp_utc="20260102",
                box_subid="abc123",
                name="a-box",
                storage_location=SL,
                creator_hostname="host",
                groups=[],
                unknown_keys={"groups": ["shadow"]},
            )

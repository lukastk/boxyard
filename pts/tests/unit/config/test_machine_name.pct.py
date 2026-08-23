# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for `machine_name` and config forward compatibility
#
# `machine_name` is how a machine identifies itself for box write-ownership —
# the value that will be written as a box's `write_owner`. It is configured and
# never derived: `get_hostname()` cannot serve as an identity, since one machine
# in this fleet has reported both `lukas-pocket4` and `pocket4`, and macOS
# reports user-editable pretty names like `Lukas’s MacBook Pro`.
#
# It is optional, so that installing this version does not break a machine whose
# config has not been rendered with a name yet. A machine without a name can
# simply never own a box, which is the safe direction.
#
# The second half of this file covers the config's unknown-key passthrough,
# which is what makes adding a key like `machine_name` survivable in the first
# place — for the NEXT such key, not this one.

# %%
#|default_exp unit.config.test_machine_name

# %%
#|export
from pathlib import Path

import pytest
import tomli_w
from pydantic import ValidationError

from boxyard import const
from boxyard.config import Config, get_config


BASE_CONFIG = {
    "default_storage_location": "default",
    "boxyard_data_path": "/tmp/boxyard",
    "box_timestamp_format": "date_and_time",
    "user_boxes_path": "/tmp/boxes",
    "user_box_groups_path": "/tmp/box-groups",
    "storage_locations": {
        "default": {"storage_type": "local", "store_path": "/tmp/store"}
    },
    "box_groups": {},
    "virtual_box_groups": {},
    "default_box_groups": [],
    "box_subid_character_set": "abcdefghijklmnopqrstuvwxyz0123456789",
    "box_subid_length": 5,
    "max_concurrent_rclone_ops": 3,
}


def write_config(tmp_path, **overrides):
    path = tmp_path / "config.toml"
    path.write_text(tomli_w.dumps({**BASE_CONFIG, **overrides}))
    return path


# ============================================================================
# The key itself
# ============================================================================

# %%
#|export
class TestMachineNameConfigKey:
    def test_absent_by_default(self, tmp_path, monkeypatch):
        """
        A config with no `machine_name` must still load. Making the key
        required would break every machine on upgrade, before myrig has
        rendered one.
        """
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)
        assert get_config(write_config(tmp_path)).machine_name is None

    def test_read_from_the_config_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)
        config = get_config(write_config(tmp_path, machine_name="mymain"))
        assert config.machine_name == "mymain"

    @pytest.mark.parametrize(
        "name", ["mymain", "macbook", "macstudio", "ideapad", "pocket4", "termux"]
    )
    def test_accepts_the_fleet_names(self, tmp_path, monkeypatch, name):
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)
        assert get_config(write_config(tmp_path, machine_name=name)).machine_name == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "x" * 65,
            "Lukas’s MacBook Pro",  # what macOS actually reports
            "has space",
            "has/slash",
            "has.dot",
        ],
    )
    def test_rejects_a_name_that_could_never_be_a_write_owner(self, name):
        """
        Caught at config load, where the message can name the file, rather
        than at claim time on one machine only.
        """
        with pytest.raises(ValidationError, match="machine_name"):
            Config(config_path="/tmp/config.toml", machine_name=name, **BASE_CONFIG)


# ============================================================================
# The environment override
# ============================================================================

# %%
#|export
class TestMachineNameEnvOverride:
    """
    `BOXYARD_MACHINE_NAME` follows the `BOXYARD_CONFIG_PATH` / `BOXYARD_RCLONE`
    precedent. It is for tests and one-offs, NOT the delivery mechanism for the
    real value: the supervisor that runs the syncs sets only PATH and HOME, so
    a name exported from an interactive shell would never reach the processes
    that actually push.
    """

    def test_sets_the_name_when_the_config_has_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, "from-env")
        assert get_config(write_config(tmp_path)).machine_name == "from-env"

    def test_overrides_the_config_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, "from-env")
        config = get_config(write_config(tmp_path, machine_name="from-file"))
        assert config.machine_name == "from-env"

    def test_empty_value_leaves_the_config_key_in_force(self, tmp_path, monkeypatch):
        """Matches the DEFAULT_BOX_GROUPS handling: empty means unset."""
        monkeypatch.setenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, "")
        config = get_config(write_config(tmp_path, machine_name="from-file"))
        assert config.machine_name == "from-file"

    def test_an_invalid_value_is_rejected_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, "not a machine name")
        with pytest.raises(ValidationError, match="machine_name"):
            get_config(write_config(tmp_path))


# ============================================================================
# Forward compatibility: a key this version does not know
# ============================================================================

# %%
#|export
class TestUnknownConfigKeys:
    """
    `config.toml` is the same trap `boxmeta.toml` was, with a wider blast
    radius: `Config` is a StrictModel, and on this fleet the file is one
    myrig-rendered artefact shared by every machine, so a key added for a
    newer boxyard would break EVERY command on EVERY machine that does not
    know it, all at once.
    """

    def test_an_unknown_key_does_not_break_loading(self, tmp_path, monkeypatch):
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)
        config = get_config(write_config(tmp_path, a_key_from_the_future="x"))
        assert config.unknown_keys == {"a_key_from_the_future": "x"}

    def test_known_keys_are_not_swallowed_into_the_passthrough(self, tmp_path, monkeypatch):
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)
        config = get_config(
            write_config(tmp_path, machine_name="mymain", a_key_from_the_future="x")
        )
        assert config.machine_name == "mymain"
        assert config.unknown_keys == {"a_key_from_the_future": "x"}

    def test_a_plain_config_has_an_empty_passthrough(self, tmp_path, monkeypatch):
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)
        assert get_config(write_config(tmp_path)).unknown_keys == {}

    def test_the_container_key_itself_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)
        path = write_config(tmp_path, unknown_keys={"foo": "bar"})
        with pytest.raises(ValueError, match="reserved key"):
            get_config(path)

    def test_a_passthrough_shadowing_a_known_field_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown_keys"):
            Config(
                config_path="/tmp/config.toml",
                unknown_keys={"machine_name": "shadow"},
                **BASE_CONFIG,
            )

    def test_a_still_invalid_known_key_is_still_rejected(self, tmp_path, monkeypatch):
        """
        Tolerating UNKNOWN keys must not weaken validation of known ones --
        otherwise the passthrough would be `extra="allow"` by another name.
        """
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)
        path = write_config(tmp_path, machine_name="not a machine name")
        with pytest.raises(ValidationError, match="machine_name"):
            get_config(path)


# ============================================================================
# ...including one level down
# ============================================================================

# %%
#|export
class TestUnknownNestedConfigKeys:
    """
    `[storage_locations.X]`, `[box_groups.X]` and `[virtual_box_groups.X]`
    entries are StrictModels too, so tolerating only the top level would leave
    the same trap one level down. Not hypothetical: `symlink_name` was added to
    both group models in `8d9e074`.
    """

    def _config(self, tmp_path, monkeypatch, **overrides):
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)
        return get_config(write_config(tmp_path, **overrides))

    def test_unknown_key_in_a_storage_location(self, tmp_path, monkeypatch):
        config = self._config(
            tmp_path,
            monkeypatch,
            storage_locations={
                "default": {
                    "storage_type": "local",
                    "store_path": "/tmp/store",
                    "a_key_from_the_future": "x",
                }
            },
        )
        assert config.unknown_keys == {
            "storage_locations.default.a_key_from_the_future": "x"
        }
        # The entry itself still built, with its known keys intact.
        assert config.storage_locations["default"].store_path == Path("/tmp/store")

    def test_unknown_key_in_a_box_group(self, tmp_path, monkeypatch):
        config = self._config(
            tmp_path,
            monkeypatch,
            box_groups={"proj": {"symlink_name": "all/proj", "future_key": 1}},
        )
        assert config.unknown_keys == {"box_groups.proj.future_key": 1}
        assert config.box_groups["proj"].symlink_name == "all/proj"

    def test_unknown_key_in_a_virtual_box_group(self, tmp_path, monkeypatch):
        config = self._config(
            tmp_path,
            monkeypatch,
            virtual_box_groups={
                "active": {"filter_expr": "NOT archived", "future_key": True}
            },
        )
        assert config.unknown_keys == {"virtual_box_groups.active.future_key": True}
        assert config.virtual_box_groups["active"].filter_expr == "NOT archived"

    def test_the_path_says_which_entry(self, tmp_path, monkeypatch):
        """The finding has to point at the entry, not just at the file."""
        config = self._config(
            tmp_path,
            monkeypatch,
            storage_locations={
                "default": {"storage_type": "local", "store_path": "/tmp/a"},
                "other": {
                    "storage_type": "local",
                    "store_path": "/tmp/b",
                    "future_key": "x",
                },
            },
        )
        assert list(config.unknown_keys) == ["storage_locations.other.future_key"]

    def test_top_level_and_nested_are_collected_together(self, tmp_path, monkeypatch):
        config = self._config(
            tmp_path,
            monkeypatch,
            top_level_future_key="a",
            box_groups={"proj": {"nested_future_key": "b"}},
        )
        assert config.unknown_keys == {
            "top_level_future_key": "a",
            "box_groups.proj.nested_future_key": "b",
        }

    def test_a_still_invalid_nested_key_is_still_rejected(self, tmp_path, monkeypatch):
        """
        Tolerating unknown nested keys must not weaken validation of the known
        ones -- `filter_expr` is compiled at load precisely so a typo surfaces
        here rather than during symlink building.
        """
        with pytest.raises(ValidationError, match="filter_expr"):
            self._config(
                tmp_path,
                monkeypatch,
                virtual_box_groups={"active": {"filter_expr": "(a AND b"}},
            )

    def test_a_non_table_entry_stays_a_loud_error(self, tmp_path, monkeypatch):
        """
        The boundary of the tolerance, pinned deliberately.

        Appending a line to the END of config.toml lands it INSIDE whatever
        table came last -- `virtual_box_groups` here -- so it arrives as an
        entry whose value is a scalar rather than a group. That is not a newer
        boxyard adding an option (that would be a field inside an entry, or a
        new top-level key, both of which ARE tolerated); it is a line the
        author believed they had added at top level. Swallowing it would
        silently discard their edit, so it raises, and the message names the
        exact path.
        """
        monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)
        path = write_config(tmp_path)
        with open(path, "a") as f:
            f.write('\na_key_from_the_future = "x"\n')

        with pytest.raises(ValidationError) as excinfo:
            get_config(path)
        assert "virtual_box_groups.a_key_from_the_future" in str(excinfo.value)

    def test_the_tables_to_walk_are_derived_not_hardcoded(self):
        """
        A config model added later must be covered without anyone remembering
        to update a list -- forgetting would reintroduce the gap silently.
        """
        from boxyard.config import (
            BoxGroupConfig,
            StorageConfig,
            VirtualBoxGroupConfig,
            _nested_model_tables,
        )

        assert _nested_model_tables() == {
            "storage_locations": StorageConfig,
            "box_groups": BoxGroupConfig,
            "virtual_box_groups": VirtualBoxGroupConfig,
        }

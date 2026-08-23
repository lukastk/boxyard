# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for `machine_name`
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

# %%
#|default_exp unit.config.test_machine_name

# %%
#|export
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

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit tests for sync policy resolution
#
# Three properties matter more than the rest, and each has bitten a previous
# design in this repo:
#
# 1. **An absent policy config changes nothing.** Every box is always due. A
#    machine that has not opted in must not start silently skipping boxes.
# 2. **Resolution is per DIMENSION.** A box overriding only its cadence keeps
#    the group policy's `compress`.
# 3. **Ambiguity raises.** Two policies disagreeing on one dimension is an
#    error, not a join -- but two policies AGREEING is not an error, which is
#    what makes `archived`+`dormant` both mapping to `cold` work.

# %%
#|default_exp unit.sync_policy.test_policy_resolution

# %%
#|export
from pathlib import Path

import pytest

from boxyard._enums import BoxPart
from boxyard._models import BoxMeta
from boxyard._sync_policy import (
    PolicyConflict,
    read_box_sync_override,
    resolve_policy,
    matching_policies,
)
from boxyard.config import Config, SyncPolicyConfig, parse_interval


BASE_CONFIG = {
    "config_path": Path("/tmp/does-not-matter/config.toml"),
    "default_storage_location": "remote",
    "boxyard_data_path": Path("/tmp/boxyard-test"),
    "box_timestamp_format": "date_only",
    "user_boxes_path": Path("/tmp/boxes"),
    "user_box_groups_path": Path("/tmp/box-groups"),
    "storage_locations": {
        "remote": {"storage_type": "local", "store_path": "/tmp/store"}
    },
    "box_groups": {},
    "virtual_box_groups": {},
    "default_box_groups": [],
    "box_subid_character_set": "abcdefghijklmnopqrstuvwxyz0123456789",
    "box_subid_length": 5,
    "max_concurrent_rclone_ops": 3,
}


def make_config(sync_policies=None, **overrides) -> Config:
    data = dict(BASE_CONFIG)
    data.update(overrides)
    if sync_policies is not None:
        data["sync_policies"] = sync_policies
    return Config(**data)


def make_box(groups=None, name="a-box") -> BoxMeta:
    return BoxMeta(
        creation_timestamp_utc="20260822",
        box_subid="aaaaa",
        name=name,
        storage_location="remote",
        creator_hostname="host",
        groups=list(groups or []),
        parents=[],
    )


# The fleet's real shape, so the tests are about the actual configuration
# rather than an invented one: cold is archived+dormant, and NOT null.
FLEET_POLICIES = {
    "default": {"data_interval": "6h", "meta_interval": "15m", "compress": False},
    "cold": {
        "data_interval": "7d",
        "compress": True,
        "groups": ["archived", "dormant"],
    },
}


# %%
#|export
def test_no_policy_config_means_every_box_always_due():
    """The opt-in property: no policies configured -> no cadence at all."""
    config = make_config()
    resolved = resolve_policy(config, make_box(groups=["proj"]))
    assert resolved.data_interval_seconds is None
    assert resolved.meta_interval_seconds is None
    assert resolved.compress is False


def test_default_policy_applies_to_an_unmatched_box():
    config = make_config(FLEET_POLICIES)
    resolved = resolve_policy(config, make_box(groups=["proj"]))
    assert resolved.data_interval_seconds == 6 * 3600
    assert resolved.meta_interval_seconds == 15 * 60
    assert resolved.compress is False
    assert resolved.sources["data_interval"] == "sync_policies.default"


def test_group_policy_beats_the_default():
    config = make_config(FLEET_POLICIES)
    resolved = resolve_policy(config, make_box(groups=["proj", "archived"]))
    assert resolved.data_interval_seconds == 7 * 86400
    assert resolved.compress is True
    assert resolved.sources["data_interval"] == "sync_policies.cold"


def test_resolution_is_per_dimension_not_per_policy():
    """
    `cold` states no meta_interval, so META must still come from `default`
    even though DATA came from `cold`. Resolving whole policies instead of
    dimensions would leave META with no cadence at all.
    """
    config = make_config(FLEET_POLICIES)
    resolved = resolve_policy(config, make_box(groups=["archived"]))
    assert resolved.data_interval_seconds == 7 * 86400
    assert resolved.sources["data_interval"] == "sync_policies.cold"
    assert resolved.meta_interval_seconds == 15 * 60
    assert resolved.sources["meta_interval"] == "sync_policies.default"


def test_two_policies_agreeing_is_not_a_conflict():
    """
    The real case: `archived` and `dormant` both map to `cold`. Matching one
    policy twice is being asked for one thing twice, not an ambiguity.
    """
    config = make_config(FLEET_POLICIES)
    resolved = resolve_policy(config, make_box(groups=["archived", "dormant"]))
    assert resolved.data_interval_seconds == 7 * 86400
    assert resolved.compress is True


def test_two_policies_disagreeing_raises_and_names_both():
    config = make_config(
        {
            "default": {"data_interval": "6h", "compress": False},
            "cold": {"data_interval": "7d", "groups": ["archived"]},
            "hot": {"data_interval": "1h", "groups": ["live"]},
        }
    )
    with pytest.raises(PolicyConflict) as excinfo:
        resolve_policy(config, make_box(groups=["archived", "live"]))
    message = str(excinfo.value)
    assert "cold" in message and "hot" in message
    assert "data_interval" in message
    # It must say what to DO, not merely that something is wrong.
    assert "conf/sync.toml" in message


def test_a_policy_stating_nothing_for_a_dimension_does_not_conflict():
    """
    `cold` sets no `compress`; `slow` sets no `compress` either. Neither states
    it, so the default answers and there is nothing to disagree about.
    """
    config = make_config(
        {
            "default": {"data_interval": "6h", "compress": False},
            "cold": {"data_interval": "7d", "groups": ["archived"]},
            "slow": {"data_interval": "7d", "groups": ["dormant"]},
        }
    )
    resolved = resolve_policy(config, make_box(groups=["archived", "dormant"]))
    assert resolved.compress is False
    assert resolved.data_interval_seconds == 7 * 86400


def test_default_policy_is_never_counted_as_a_match():
    """
    If `default` were treated as a matching policy, a box in a group that
    `default` happened to list would look ambiguous against every other policy.
    """
    config = make_config(
        {
            "default": {"data_interval": "6h", "groups": ["archived"]},
            "cold": {"data_interval": "7d", "groups": ["archived"]},
        }
    )
    assert set(matching_policies(config, make_box(groups=["archived"]))) == {"cold"}
    resolved = resolve_policy(config, make_box(groups=["archived"]))
    assert resolved.data_interval_seconds == 7 * 86400


# %% [markdown]
# ## Per-box override (`conf/sync.toml`)
#
# These exist because mutation testing found the whole override path untested:
# disabling it left the suite green. It is the feature Lukas asked for by name
# ("set these things box-by-box"), so it gets the most cases.

# %%
#|export
def write_box_conf(tmp_path: Path, box_meta: BoxMeta, config: Config, text: str) -> Path:
    conf_dir = box_meta.get_local_part_path(config, BoxPart.CONF)
    conf_dir.mkdir(parents=True, exist_ok=True)
    path = conf_dir / "sync.toml"
    path.write_text(text)
    return path


def config_at(tmp_path: Path, sync_policies=None) -> Config:
    return make_config(
        sync_policies,
        boxyard_data_path=tmp_path / "boxyard",
        storage_locations={
            "remote": {"storage_type": "local", "store_path": str(tmp_path / "store")}
        },
    )


def test_absent_box_conf_is_the_normal_case(tmp_path):
    config = config_at(tmp_path, FLEET_POLICIES)
    assert read_box_sync_override(config, make_box()) == {}


def test_box_conf_override_beats_the_group_policy(tmp_path):
    config = config_at(tmp_path, FLEET_POLICIES)
    box = make_box(groups=["archived"])
    write_box_conf(tmp_path, box, config, 'data_interval = "1h"\n')
    resolved = resolve_policy(config, box)
    assert resolved.data_interval_seconds == 3600
    assert resolved.sources["data_interval"] == "conf/sync.toml"


def test_box_conf_override_is_per_dimension(tmp_path):
    """
    Overriding only the cadence must KEEP the group policy's `compress`.
    Lukas's stated requirement: type and schedule are independent axes.
    """
    config = config_at(tmp_path, FLEET_POLICIES)
    box = make_box(groups=["archived"])
    write_box_conf(tmp_path, box, config, 'data_interval = "1h"\n')
    resolved = resolve_policy(config, box)
    assert resolved.data_interval_seconds == 3600
    assert resolved.compress is True
    assert resolved.sources["compress"] == "sync_policies.cold"


def test_box_conf_can_override_compress_alone(tmp_path):
    config = config_at(tmp_path, FLEET_POLICIES)
    box = make_box(groups=["archived"])
    write_box_conf(tmp_path, box, config, "compress = false\n")
    resolved = resolve_policy(config, box)
    assert resolved.compress is False
    assert resolved.data_interval_seconds == 7 * 86400


def test_box_conf_settles_a_policy_conflict(tmp_path):
    """
    The escape hatch the conflict message tells the user about must actually
    work -- otherwise the error names a fix that does not exist.
    """
    config = config_at(
        tmp_path,
        {
            "default": {"data_interval": "6h"},
            "cold": {"data_interval": "7d", "groups": ["archived"]},
            "hot": {"data_interval": "1h", "groups": ["live"]},
        },
    )
    box = make_box(groups=["archived", "live"])
    with pytest.raises(PolicyConflict):
        resolve_policy(config, box)
    write_box_conf(tmp_path, box, config, 'data_interval = "2h"\n')
    assert resolve_policy(config, box).data_interval_seconds == 7200


def test_box_conf_with_an_unknown_key_is_refused(tmp_path):
    config = config_at(tmp_path, FLEET_POLICIES)
    box = make_box()
    write_box_conf(tmp_path, box, config, 'data_intervl = "1h"\n')
    with pytest.raises(ValueError, match="unknown key"):
        resolve_policy(config, box)


def test_box_cannot_set_groups(tmp_path):
    """`groups` is a policy-level concept; a box claiming one would invert the mapping."""
    config = config_at(tmp_path, FLEET_POLICIES)
    box = make_box()
    write_box_conf(tmp_path, box, config, 'groups = ["archived"]\n')
    with pytest.raises(ValueError, match="groups"):
        resolve_policy(config, box)


def test_box_conf_that_is_not_valid_toml_is_refused_loudly(tmp_path):
    """
    A deliberately-written file that cannot be read must never be silently
    ignored: that would apply a cadence its author did not ask for.
    """
    config = config_at(tmp_path, FLEET_POLICIES)
    box = make_box()
    write_box_conf(tmp_path, box, config, "data_interval = \n")
    with pytest.raises(ValueError, match="not valid TOML"):
        resolve_policy(config, box)


def test_box_conf_with_a_bad_interval_names_the_box_conf(tmp_path):
    config = config_at(tmp_path, FLEET_POLICIES)
    box = make_box()
    write_box_conf(tmp_path, box, config, 'data_interval = "soon"\n')
    with pytest.raises(ValueError, match="conf/sync.toml"):
        resolve_policy(config, box)


def test_box_conf_with_a_non_string_interval_is_refused(tmp_path):
    config = config_at(tmp_path, FLEET_POLICIES)
    box = make_box()
    write_box_conf(tmp_path, box, config, "data_interval = 6\n")
    with pytest.raises(ValueError, match="must be a string"):
        resolve_policy(config, box)


def test_box_conf_with_a_non_bool_compress_is_refused(tmp_path):
    config = config_at(tmp_path, FLEET_POLICIES)
    box = make_box()
    write_box_conf(tmp_path, box, config, 'compress = "yes"\n')
    with pytest.raises(ValueError, match="must be true or false"):
        resolve_policy(config, box)

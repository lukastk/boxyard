# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # The config pin is what gates the rollout
#
# The decoupled rollout deploys the restic-capable code everywhere FIRST with
# the behaviour still switched off, and flips the behaviour later as a separate
# act. What holds the behaviour off is one stanza in myrig's config template:
#
# ```toml
# [sync_policies.default]
# storage_format = "plain"
# ```
#
# and deleting that one line IS the flip. So the whole plan rests on a claim
# that had never been exercised in this direction: that config BEATS the
# `DEFAULT_STORAGE_FORMAT` module constant. Every existing test goes the other
# way -- config absent, constant answering.
#
# These four tests establish the mechanism in both directions, and separate two
# cases that look alike and are not:
#
# | config state | new box on a remote location |
# |---|---|
# | `[sync_policies.default]` with `storage_format = "plain"` | **plain** -- pinned |
# | that stanza deleted | **restic** -- the flip |
# | no `[sync_policies.*]` at all | **restic** -- the constant answers |
# | `[sync_policies.default]` present but WITHOUT `storage_format` | **restic** -- the constant answers |
#
# The bottom two rows are the ones to be careful about. An absent table does NOT
# mean "pinned", and neither does a table that merely exists. Only the
# `storage_format` KEY pins anything, so the rollout has to ADD that key rather
# than rely on any machine's current state -- and every machine today is in one
# of those two bottom rows.

# %%
#|default_exp integration.cmds.test_rollout_pin

# %%
#|export
import tomllib
from pathlib import Path

import tomli_w

import pytest

from boxyard._enums import BoxPart, StorageFormat
from boxyard._models import get_boxyard_meta
from boxyard._sync_policy import DEFAULT_STORAGE_FORMAT, resolve_policy
from boxyard.cmds import new_box
from boxyard.config import get_config

pytestmark = [pytest.mark.integration]


# %%
#|export
@pytest.fixture
def remote_yard(monkeypatch, tmp_path):
    """
    One yard whose storage location is of type `rclone` -- i.e. the case the
    module constant answers RESTIC for. A `local` location would default to
    plain for its own reasons and would prove nothing about the pin.

    The password is set because creating a restic box requires one; without it
    `new_box` refuses, and these tests would pass for the wrong reason.
    """
    from tests.integration.conftest import create_boxyards

    monkeypatch.setenv("BOXYARD_RESTIC_PASSWORD", "rollout-pin-test-password")
    # With `num_boxyards=1` this returns the flattened 5-tuple, not a list.
    remote_name, _remote_root, _cfg, config_path, _data = create_boxyards(
        num_boxyards=1
    )
    return {"config_path": config_path, "remote_name": remote_name}


def set_policies(config_path, policies):
    """Rewrite the config's `[sync_policies]` table wholesale.

    `policies=None` removes the table entirely, which is what every machine in
    the fleet looks like today.
    """
    dump = tomllib.loads(Path(config_path).read_text())
    if policies is None:
        dump.pop("sync_policies", None)
    else:
        dump["sync_policies"] = policies
    Path(config_path).write_text(tomli_w.dumps(dump))


def create_and_read_format(yard, box_name):
    """Create a box; report the stamped format and the boxmeta as written.

    Read back from the boxmeta on disk, not from the return value: what governs
    the box for the rest of its life is the stamped field, and a test that
    asserted on a freshly resolved policy could pass while nothing was written.
    """
    config_path = yard["config_path"]
    idx = new_box(config_path=config_path, box_name=box_name,
                  storage_location=yard["remote_name"], claim=False)
    config = get_config(config_path)
    box = get_boxyard_meta(config).by_index_name[idx]
    on_disk = tomllib.loads(
        box.get_local_part_path(config, BoxPart.META).read_text()
    )
    return box.storage_format, on_disk, config, box


# %% [markdown]
# ## The pair the rollout is made of
#
# These two are the whole plan: the first is the deploy, the second is the flip.

# %%
#|export
def test_the_config_pin_beats_the_module_constant(remote_yard):
    """
    THE load-bearing test. With `storage_format = "plain"` in config, a new box
    on a remote storage location is created PLAIN even though the module
    constant says RESTIC.

    The assertion on the constant is not decoration: without it a future edit
    setting `DEFAULT_STORAGE_FORMAT = PLAIN` would leave this test passing while
    proving nothing at all about the pin.
    """
    assert DEFAULT_STORAGE_FORMAT is StorageFormat.RESTIC, (
        "this test only means something while the constant disagrees with the pin"
    )
    set_policies(remote_yard["config_path"],
                 {"default": {"storage_format": "plain"}})

    stamped, on_disk_keys, config, box = create_and_read_format(
        remote_yard, "pinned"
    )

    assert stamped is StorageFormat.PLAIN
    assert resolve_policy(config, box).storage_format is StorageFormat.PLAIN
    assert box.get_local_part_path(config, BoxPart.DATA).is_dir(), (
        "a plain box keeps a real DATA directory -- the point of pinning"
    )
    # A plain box OMITS the key rather than writing `storage_format = "plain"`
    # (`_models.py`: "a plain box writes the file every earlier version
    # wrote"). That is worth more to this rollout than a written key would be:
    # during the pin window a 0.7.0 machine produces boxmetas byte-identical to
    # 0.6.x's, so pocket4 sees nothing new at all -- not even the
    # `unknown-boxmeta-keys` that a restic box would give it.
    assert "storage_format" not in on_disk_keys, (
        "the pin must leave the boxmeta indistinguishable from a 0.6.x one"
    )


def test_deleting_the_pin_is_the_flip(remote_yard):
    """
    The same yard and the same creation with the stanza removed yields RESTIC.
    Paired with the test above deliberately: together they are the rollout, and
    the only difference between them is the one line a person deletes.
    """
    set_policies(remote_yard["config_path"], {"default": {}})

    stamped, on_disk, _config, _box = create_and_read_format(
        remote_yard, "unpinned"
    )

    assert stamped is StorageFormat.RESTIC
    assert on_disk["storage_format"] == "restic", (
        "a restic box must WRITE the key -- that is what tells a stale machine "
        "something is different, via `unknown-boxmeta-keys`"
    )


# %% [markdown]
# ## What an ABSENT policy table means -- and it is not "pinned"
#
# Every machine in the fleet today has no `[sync_policies.*]` at all. If that
# state meant "plain" the rollout would need no template change; it does not.

# %%
#|export
def test_no_policy_table_at_all_means_the_module_constant(remote_yard):
    """
    Answers the rollout question directly: an absent table means CONSTANT, so
    on 0.7.0 it means RESTIC. This is the unsafe reading of the two, and it is
    the one that is true.

    So the pin must be ADDED to every machine's config. Deploying 0.7.0 without
    the template change would flip the default on the four reachable machines
    the moment it landed -- which is exactly the coupling the decoupled rollout
    exists to avoid.
    """
    set_policies(remote_yard["config_path"], None)

    config = get_config(remote_yard["config_path"])
    assert config.sync_policies == {}, "precondition: this is a fleet machine today"

    stamped, on_disk, _config, _box = create_and_read_format(
        remote_yard, "notable"
    )

    assert stamped is StorageFormat.RESTIC
    assert on_disk["storage_format"] == "restic", (
        "a restic box must WRITE the key -- that is what tells a stale machine "
        "something is different, via `unknown-boxmeta-keys`"
    )


def test_a_policy_table_without_the_key_also_means_the_module_constant(remote_yard):
    """
    The sharper case, and the one a person is most likely to get wrong: a
    machine that already carries a `[sync_policies.default]` for CADENCE is not
    pinned. `None` means "not stated at this level" per dimension, so an
    unrelated policy stanza gives no protection whatsoever.

    A rollout check of the shape "does this machine have a `[sync_policies]`
    table?" would therefore pass on a machine that is about to create restic
    boxes. The check has to be for the KEY.
    """
    set_policies(remote_yard["config_path"],
                 {"default": {"data_interval": "6h"}})

    config = get_config(remote_yard["config_path"])
    assert config.sync_policies["default"].storage_format is None
    assert config.sync_policies["default"].data_interval == "6h"

    stamped, on_disk, _config, _box = create_and_read_format(
        remote_yard, "cadenceonly"
    )

    assert stamped is StorageFormat.RESTIC
    assert on_disk["storage_format"] == "restic", (
        "a restic box must WRITE the key -- that is what tells a stale machine "
        "something is different, via `unknown-boxmeta-keys`"
    )


# %% [markdown]
# ## The check a person actually runs at each rollout step
#
# The rollout needs a way to ask a machine "are you pinned?" that does not
# amount to grepping a TOML file for a string. `boxyard doctor` already answers
# it end to end, through the same `resolve_policy` the creation path uses:
#
# - **pinned** -> policy says plain, every box is plain -> NO
#   `storage-format-mismatch`
# - **not pinned** -> policy says restic, every box is plain -> the finding
#   fires, on every box
#
# So `storage-format-mismatch` doubles as the rollout's readiness signal. These
# tests verify that, because a documented check nobody has run is not a check.

# %%
#|export
def doctor_findings(config_path):
    import asyncio

    from boxyard.cmds import run_doctor

    report = asyncio.run(run_doctor(config_path=config_path, check_remote=False))
    # `report["checks"]` carries EVERY check name whether or not it found
    # anything, so `"x" in report["checks"]` is always true and proves nothing.
    # Return the findings themselves.
    return {
        name: entry["findings"]
        for name, entry in report["checks"].items()
        if entry["findings"]
    }


def test_a_pinned_machine_reports_no_format_mismatch(remote_yard):
    """What step 1 and step 2 look like when they pass."""
    set_policies(remote_yard["config_path"],
                 {"default": {"storage_format": "plain"}})
    create_and_read_format(remote_yard, "pinnedbox")

    assert "storage-format-mismatch" not in doctor_findings(
        remote_yard["config_path"]
    ), "a pinned machine has no gap between policy and box"


def test_an_unpinned_machine_reports_a_mismatch_for_every_plain_box(remote_yard):
    """
    The same yard with the pin removed. This is BOTH the failure signal for
    step 1 -- a machine that was deployed without the template change -- and
    the expected state after step 4, where it stops being a warning and becomes
    the migration backlog.

    Which is why the release note has to say so: after the flip this finding
    fires on ~596 boxes at once, and its length must not be read as breakage.
    """
    set_policies(remote_yard["config_path"],
                 {"default": {"storage_format": "plain"}})
    create_and_read_format(remote_yard, "boxone")
    create_and_read_format(remote_yard, "boxtwo")

    set_policies(remote_yard["config_path"], None)

    findings = doctor_findings(remote_yard["config_path"])
    assert "storage-format-mismatch" in findings
    messages = " ".join(str(m) for m in findings["storage-format-mismatch"])
    assert "boxone" in messages and "boxtwo" in messages, (
        "it fires per box, not once -- that is what makes it a backlog"
    )


# %% [markdown]
# ## Why the pinned config is safe to deploy to a machine still on 0.6.x
#
# The rollout puts `storage_format` into a config that may reach a machine whose
# `SyncPolicyConfig` has never heard of it — pocket4, when it comes back and
# myrig writes config before the package upgrade lands. `SyncPolicyConfig` is a
# StrictModel with `extra="forbid"`, so the obvious fear is that such a machine
# refuses to load its config at all and every boxyard command on it breaks.
#
# It does not, and not by luck: `_split_known_keys` diverts keys a model does
# not declare into `Config.unknown_keys`, and it covers nested
# `dict[str, StrictModel]` tables — `sync_policies` among them — derived from
# the annotations rather than a hardcoded list.
#
# MEASURED against the pre-restic build (`ef04aa3`), given the pinned config:
# it LOADS, the key arrives as `unknown_keys={'sync_policies.default.
# storage_format': 'plain'}`, and its `boxyard doctor` reports
# `unknown-config-keys` naming it. A signal, not a break.
#
# This test pins the property in THIS build, because what would silently undo
# the rollout's safety is someone making config parsing strict again.

# %%
#|export
def test_an_unknown_policy_key_is_tolerated_not_fatal(remote_yard):
    """
    A key `SyncPolicyConfig` does not declare must land in `unknown_keys` and
    leave the config loadable -- which is what lets a pinned config reach a
    machine that predates the key.
    """
    set_policies(remote_yard["config_path"],
                 {"default": {"storage_format": "plain",
                              "a_key_from_the_future": "x"}})

    config = get_config(remote_yard["config_path"])

    assert config.unknown_keys == {
        "sync_policies.default.a_key_from_the_future": "x"
    }, "an unknown policy key must be parked, not rejected and not dropped"
    assert (
        config.sync_policies["default"].storage_format is StorageFormat.PLAIN
    ), "and the keys it DOES know must still take effect"

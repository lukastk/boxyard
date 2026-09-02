# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `doctor`'s `storage-format-mismatch`
#
# Policy says what format a box SHOULD have; `boxmeta.storage_format` says what
# it actually has. Only an explicit conversion moves the second, so a gap is a
# normal state during migration and doctor's job is to make it visible — not to
# close it, and above all not to let a `config.toml` edit close it silently.

# %%
#|default_exp integration.cmds.test_doctor_storage_format

# %%
#|export
import asyncio
import tomllib
from pathlib import Path

import pytest
import tomli_w

from boxyard._enums import StorageFormat
from boxyard._models import BoxMeta
from boxyard.cmds import modify_boxmeta, new_box, run_doctor
from boxyard.config import get_config

pytestmark = pytest.mark.integration


# %%
#|export
@pytest.fixture
def yard(temp_boxyard):
    remote_name, _remote_path, _config, config_path, _data_path = temp_boxyard
    index_name = new_box(
        config_path=config_path,
        box_name="fmtbox",
        storage_location=remote_name,
        claim=False,
    )
    modify_boxmeta(
        config_path=config_path,
        box_index_name=index_name,
        modifications={"groups": ["archived"]},
    )
    return {
        "remote_name": remote_name,
        "config_path": config_path,
        "index_name": index_name,
    }


def set_policies(yard, policies):
    path = Path(yard["config_path"])
    with open(path, "rb") as f:
        parsed = tomllib.load(f)
    parsed["sync_policies"] = policies
    path.write_text(tomli_w.dumps(parsed))
    return get_config(path)


def findings(yard, name):
    report = asyncio.run(
        run_doctor(config_path=yard["config_path"], check_remote=False)
    )
    return report["checks"][name]["findings"]


def load(yard):
    config = get_config(yard["config_path"])
    return config, BoxMeta.load(config, yard["remote_name"], yard["index_name"])


# %% [markdown]
# ## It is silent on a yard that has not opted in

# %%
#|export
def test_no_finding_on_a_default_yard(yard):
    """
    The default is `plain`, every box is `plain`, and no policy sets the key —
    so this check must fire on nothing at all until someone configures it.
    """
    assert findings(yard, "storage-format-mismatch") == []


def test_no_finding_when_policy_and_reality_agree(yard):
    set_policies(yard, {"default": {"storage_format": "plain"}})
    assert findings(yard, "storage-format-mismatch") == []


# %% [markdown]
# ## It reports the gap, and does not close it

# %%
#|export
def test_a_policy_asking_for_restic_is_reported(yard):
    set_policies(yard, {"cold": {"groups": ["archived"], "storage_format": "restic"}})
    found = findings(yard, "storage-format-mismatch")
    assert len(found) == 1
    assert yard["index_name"] in found[0]["message"]
    assert "plain" in found[0]["message"] and "restic" in found[0]["message"]


def test_the_finding_names_where_the_intent_came_from(yard):
    """
    Otherwise the user has to reverse-engineer the resolution order to find out
    which policy is asking.
    """
    set_policies(yard, {"cold": {"groups": ["archived"], "storage_format": "restic"}})
    assert "sync_policies.cold" in findings(yard, "storage-format-mismatch")[0]["message"]


def test_running_doctor_does_not_convert_anything(yard):
    """
    doctor REPORTS. A check that quietly fixed the primary copy of everything
    would be the `compress` defect with a different name.
    """
    set_policies(yard, {"default": {"storage_format": "restic"}})
    _config, before = load(yard)
    assert before.storage_format is StorageFormat.PLAIN

    findings(yard, "storage-format-mismatch")

    _config, after = load(yard)
    assert after.storage_format is StorageFormat.PLAIN


def test_the_reverse_direction_is_reported_too(yard):
    """
    A restic-backed box under a `plain` policy. More dangerous than the forward
    case: acting on it would overwrite a repository with a directory tree.
    """
    config, meta = load(yard)
    meta.storage_format = StorageFormat.RESTIC
    meta.save(config)
    set_policies(yard, {"default": {"storage_format": "plain"}})

    found = findings(yard, "storage-format-mismatch")
    assert len(found) == 1
    _config, after = load(yard)
    assert after.storage_format is StorageFormat.RESTIC, "still not converted"


# %% [markdown]
# ## It does not double-report a box doctor already complained about

# %%
#|export
def test_a_conflicted_policy_is_reported_once_not_twice(yard):
    """
    A box whose policies disagree is already `sync-policy-conflict`. Reporting
    it again under a second name would make doctor noisier without saying
    anything new — and doctor is only useful while people still read it.
    """
    set_policies(
        yard,
        {
            "a": {"groups": ["archived"], "storage_format": "restic"},
            "b": {"groups": ["archived"], "storage_format": "plain"},
        },
    )
    assert len(findings(yard, "sync-policy-conflict")) == 1
    assert findings(yard, "storage-format-mismatch") == []


# %% [markdown]
# ## The hint names a real command, and warns about the fleet
#
# `diverged-box` spent months telling people to run something that exited 2.
# `test_doctor_hints_are_runnable` enforces that every named command parses;
# this pins that the hint carries the two things a person needs to act safely.

# %%
#|export
def test_the_hint_names_the_conversion_command(yard):
    set_policies(yard, {"default": {"storage_format": "restic"}})
    hint = findings(yard, "storage-format-mismatch")[0]["hint"]
    assert f"boxyard convert -r '{yard['index_name']}'" in hint
    assert "--dry-run" in hint


def test_the_hint_warns_that_old_machines_cannot_read_a_converted_box(yard):
    """
    The rollout constraint. Someone reading this finding is exactly the person
    about to convert, and it is the one thing they must know first.
    """
    set_policies(yard, {"default": {"storage_format": "restic"}})
    hint = findings(yard, "storage-format-mismatch")[0]["hint"]
    assert "older boxyard" in hint
    assert "whole fleet is" in hint


# %% [markdown]
# ## The hint has to name a command that can actually help
#
# `boxyard convert` only goes plain -> restic. Offering it for a mismatch in the
# OTHER direction sends someone to a command that cannot do what they need --
# and that direction is not exotic: it is what every converted box reports
# during the rollout's pinned window, when the policy deliberately says `plain`.

# %%
#|export
def hint_for(yard):
    found = findings(yard, "storage-format-mismatch")
    assert len(found) == 1, f"expected one mismatch, got {found}"
    return found[0]["hint"]


def test_the_hint_for_a_plain_box_offers_convert(yard):
    """plain box, restic policy -- `boxyard convert` is exactly the fix."""
    set_policies(yard, {"default": {"storage_format": "restic"}})
    hint = hint_for(yard)
    assert "boxyard convert" in hint
    assert "NO SUPPORTED ROUTE BACK" not in hint


def test_the_hint_for_a_restic_box_says_there_is_no_route_back(yard):
    """
    restic box, plain policy. The honest answer is that there is none: no
    `--to-plain` exists, and copying out into a new box does not preserve the
    box's id, groups or attachments.
    """
    config, box = load(yard)
    box.storage_format = StorageFormat.RESTIC
    box.save(config)
    set_policies(yard, {"default": {"storage_format": "plain"}})

    hint = hint_for(yard)
    assert "NO SUPPORTED ROUTE BACK" in hint
    assert "boxyard copy" in hint, "it must name the only route that exists"
    assert "--to-plain" in hint, "and say plainly that the flag does not exist"

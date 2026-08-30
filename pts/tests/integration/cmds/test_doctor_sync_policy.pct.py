# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `doctor` reports sync-policy problems
#
# Resolving a cadence REFUSES ambiguity rather than joining it. `multi-sync`
# says so on stderr and syncs the box anyway, but an unattended loop's stderr
# is not where a configuration mistake should live -- so doctor names it too.
#
# By design these fire on nothing today: policy keys off LIFECYCLE groups, and
# measured across the real 590-box yard no box carries both `archived` and
# `dormant`. That makes it especially important to prove they CAN fire.

# %%
#|default_exp integration.cmds.test_doctor_sync_policy

# %%
#|export
import asyncio

import pytest
import tomli_w
import tomllib

from boxyard.cmds import modify_boxmeta, new_box, run_doctor
from boxyard._enums import BoxPart
from boxyard._models import get_boxyard_meta
from boxyard.config import get_config


def _doctor(config_path, **kwargs):
    return asyncio.run(
        run_doctor(config_path=config_path, check_remote=False, **kwargs)
    )


def _set_policies(config_path, policies):
    with open(config_path, "rb") as f:
        dump = tomllib.load(f)
    dump["sync_policies"] = policies
    config_path.write_text(tomli_w.dumps(dump))
    return get_config(config_path)


def _findings(report, name):
    return report["checks"][name]["findings"]


TORN = {
    "default": {"data_interval": "6h"},
    "cold": {"data_interval": "7d", "groups": ["archived"]},
    "hot": {"data_interval": "1h", "groups": ["live"]},
}


# %%
#|export
def test_a_clean_yard_reports_no_policy_findings(temp_boxyard):
    remote_name, _, config, config_path, _ = temp_boxyard
    new_box(config_path=config_path, box_name="a-box",
            storage_location=remote_name, claim=False)
    _set_policies(config_path, TORN)

    report = _doctor(config_path)
    assert _findings(report, "sync-policy-conflict") == []
    assert _findings(report, "unusable-box-sync-conf") == []


def test_a_torn_box_is_reported(temp_boxyard):
    remote_name, _, config, config_path, _ = temp_boxyard
    index_name = new_box(config_path=config_path, box_name="torn-box",
                         storage_location=remote_name, claim=False)
    modify_boxmeta(config_path=config_path, box_index_name=index_name,
                   modifications={"groups": ["archived", "live"]})
    _set_policies(config_path, TORN)

    findings = _findings(_doctor(config_path), "sync-policy-conflict")
    assert len(findings) == 1, findings
    assert findings[0]["box_index_name"] == index_name
    assert "cold" in findings[0]["message"] and "hot" in findings[0]["message"]
    # The hint must say what to DO, not merely that something is wrong.
    assert "conf/sync.toml" in findings[0]["hint"]


def test_two_policies_agreeing_is_not_reported(temp_boxyard):
    """`archived` and `dormant` both mapping to cold is one ask, not a conflict."""
    remote_name, _, config, config_path, _ = temp_boxyard
    index_name = new_box(config_path=config_path, box_name="cold-box",
                         storage_location=remote_name, claim=False)
    modify_boxmeta(config_path=config_path, box_index_name=index_name,
                   modifications={"groups": ["archived", "dormant"]})
    _set_policies(config_path, {
        "default": {"data_interval": "6h"},
        "cold": {"data_interval": "7d", "groups": ["archived", "dormant"]},
    })
    assert _findings(_doctor(config_path), "sync-policy-conflict") == []


def test_an_unreadable_box_sync_conf_is_reported(temp_boxyard):
    remote_name, _, config, config_path, _ = temp_boxyard
    index_name = new_box(config_path=config_path, box_name="bad-conf-box",
                         storage_location=remote_name, claim=False)
    config = _set_policies(config_path, TORN)

    meta = next(m for m in get_boxyard_meta(config).box_metas
                if m.index_name == index_name)
    conf_dir = meta.get_local_part_path(config, BoxPart.CONF)
    conf_dir.mkdir(parents=True, exist_ok=True)
    (conf_dir / "sync.toml").write_text("data_interval = \n")

    findings = _findings(_doctor(config_path), "unusable-box-sync-conf")
    assert len(findings) == 1, findings
    assert index_name in findings[0]["message"]


def test_doctor_does_not_crash_on_a_torn_box(temp_boxyard):
    """
    The reason the conflict is a FINDING and not an exception: doctor must keep
    running and report everything else, or one misconfigured box blinds the
    whole report.
    """
    remote_name, _, config, config_path, _ = temp_boxyard
    torn = new_box(config_path=config_path, box_name="torn-box",
                   storage_location=remote_name, claim=False)
    new_box(config_path=config_path, box_name="fine-box",
            storage_location=remote_name, claim=False)
    modify_boxmeta(config_path=config_path, box_index_name=torn,
                   modifications={"groups": ["archived", "live"]})
    _set_policies(config_path, TORN)

    report = _doctor(config_path)
    assert len(_findings(report, "sync-policy-conflict")) == 1
    # ...and every other check still ran.
    assert len(report["checks"]) > 10

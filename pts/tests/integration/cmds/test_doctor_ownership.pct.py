# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Doctor: `unknown-boxmeta-keys` and `machine-name-unset`
#
# The two checks v0.5.0 adds, both purely local.
#
# `unknown-boxmeta-keys` is the visible half of the forward-compat passthrough.
# Tolerating a newer key is what stops a box from vanishing; this check is what
# stops the tolerance from being silent. So the tests assert both halves — the
# finding IS raised, and the box survives everything a rejected registration
# would have cost it.
#
# `machine-name-unset` reports a machine that cannot own a box yet. It fires
# whenever the name is missing, not only once boxes are owned, so that the gap
# between installing this version and configuring the name is visible while it
# is still true rather than at the moment someone first tries to claim.

# %%
#|default_exp integration.cmds.test_doctor_ownership

# %%
#|export
import asyncio

import pytest
import tomllib
import tomli_w

from boxyard import const
from boxyard.cmds import modify_boxmeta, new_box, run_doctor
from boxyard._models import BoxMeta, get_boxyard_meta
from boxyard.config import get_config


def _doctor(config_path, **kwargs):
    return asyncio.run(run_doctor(config_path=config_path, check_remote=False, **kwargs))


def _findings(report, check_name):
    return report["checks"][check_name]["findings"]


def _boxmeta_path(config, storage_location, index_name):
    return (
        config.local_store_path
        / storage_location
        / index_name
        / const.BOX_METAFILE_REL_PATH
    )


def _add_raw_keys(config, storage_location, index_name, **keys):
    """Add keys to a boxmeta.toml as a NEWER boxyard would have written them."""
    path = _boxmeta_path(config, storage_location, index_name)
    path.write_text(tomli_w.dumps({**tomllib.loads(path.read_text()), **keys}))
    return path


def _set_config_key(config_path, key, value):
    config_dict = tomllib.loads(config_path.read_text())
    if value is None:
        config_dict.pop(key, None)
    else:
        config_dict[key] = value
    config_path.write_text(tomli_w.dumps(config_dict))


# ============================================================================
# unknown-boxmeta-keys
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_unknown_boxmeta_keys_reported(temp_boxyard):
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    plain = new_box(config_path=config_path, box_name="plain", storage_location=remote_name)
    newer = new_box(config_path=config_path, box_name="newer", storage_location=remote_name)
    _add_raw_keys(
        config, remote_name, newer, write_owner_since="2026-08-23", some_future_key=7
    )

    report = _doctor(config_path)
    findings = _findings(report, "unknown-boxmeta-keys")

    assert [f["index_name"] for f in findings] == [newer]
    assert findings[0]["unknown_keys"] == ["some_future_key", "write_owner_since"]
    assert "some_future_key" in findings[0]["message"]
    # The box that carries only keys this version knows is not reported.
    assert not any(f["index_name"] == plain for f in findings)


# %%
#|export
@pytest.mark.integration
def test_a_newer_key_does_not_cost_the_box_its_registration(temp_boxyard):
    """
    The failure this passthrough exists to prevent. Before it, an unknown key
    made `BoxMeta.load` raise, `create_boxyard_meta` SKIP the registration, and
    the box drop out of `boxyard_meta.json` -- and with it out of `boxyard
    list`, out of `~/g` (whose symlinks are then deleted) and out of
    `multi-sync`, silently, without healing on upgrade.
    """
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    newer = new_box(config_path=config_path, box_name="newer", storage_location=remote_name)
    _add_raw_keys(config, remote_name, newer, some_future_key="x")

    report = _doctor(config_path)

    # Not a broken registration...
    assert not _findings(report, "broken-registration")
    # ...and still in the cache that `list`, `~/g` and `multi-sync` all read.
    assert newer in get_boxyard_meta(config, force_create=True).by_index_name


# %%
#|export
@pytest.mark.integration
def test_a_newer_key_survives_add_to_group(temp_boxyard):
    """
    An older machine must not STRIP a newer machine's key. `add-to-group` is
    the everyday command that would do it: it rebuilds the box meta from the
    cache and saves it back.
    """
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    newer = new_box(config_path=config_path, box_name="newer", storage_location=remote_name)
    path = _add_raw_keys(config, remote_name, newer, some_future_key="x")

    modify_boxmeta(
        config_path=config_path,
        box_index_name=newer,
        modifications={"groups": ["a-new-group"]},
    )

    written = tomllib.loads(path.read_text())
    assert written["some_future_key"] == "x"
    assert written["groups"] == ["a-new-group"]


# ============================================================================
# machine-name-unset
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_machine_name_unset_reported(temp_boxyard, monkeypatch):
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard
    monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)

    # The fixture configures a name, so a configured machine reports nothing.
    assert not _findings(_doctor(config_path), "machine-name-unset")

    _set_config_key(config_path, "machine_name", None)

    report = _doctor(config_path)
    findings = _findings(report, "machine-name-unset")
    assert len(findings) == 1
    assert str(config_path) in findings[0]["message"]
    # The hint has to name both ways to set it.
    assert "machine_name" in findings[0]["hint"]
    assert const.ENV_VAR_BOXYARD_MACHINE_NAME in findings[0]["hint"]
    assert not report["healthy"]


# %%
#|export
@pytest.mark.integration
def test_machine_name_from_the_environment_satisfies_the_check(temp_boxyard, monkeypatch):
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    _set_config_key(config_path, "machine_name", None)
    assert _findings(_doctor(config_path), "machine-name-unset")

    monkeypatch.setenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, "from-env")
    assert not _findings(_doctor(config_path), "machine-name-unset")


# ============================================================================
# unknown-config-keys
# ============================================================================

# %%
#|export
@pytest.mark.integration
def test_unknown_config_keys_reported(temp_boxyard):
    """
    The other half of the config passthrough. Tolerating an unknown key
    without reporting it would trade the loud typo `extra="forbid"` catches
    today for a silent one.
    """
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    assert not _findings(_doctor(config_path), "unknown-config-keys")

    _set_config_key(config_path, "a_key_from_the_future", "x")

    report = _doctor(config_path)
    findings = _findings(report, "unknown-config-keys")
    assert len(findings) == 1
    assert findings[0]["unknown_keys"] == ["a_key_from_the_future"]
    assert str(config_path) in findings[0]["message"]
    # The hint must not assume "newer boxyard"; doctor cannot tell that from a typo.
    assert "typo" in findings[0]["hint"]
    assert not report["healthy"]


# %%
#|export
@pytest.mark.integration
def test_an_unknown_config_key_does_not_stop_doctor_running(temp_boxyard):
    """
    The point of the passthrough: before it, an unknown config key made every
    command on the machine fail -- doctor included, so the machine could not
    even be asked what was wrong with it.
    """
    remote_name, remote_rclone_path, config, config_path, data_path = temp_boxyard

    _set_config_key(config_path, "a_key_from_the_future", "x")

    report = _doctor(config_path)
    # Every other check still ran and still found nothing.
    assert not _findings(report, "broken-registration")
    assert not _findings(report, "unknown-storage-location")
    assert not _findings(report, "rclone-config")

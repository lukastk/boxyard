# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # A New Box Belongs To The Machine That Made It
#
# `new_box` never set `write_owner`, so every box created since ownership
# landed in v0.5.2 was born UNOWNED — the exact state the feature exists to
# remove. On mymain on 2026-08-27 the only unowned boxes held there were the
# three created since the claim sweep.
#
# The "unowned by default" rule was a MIGRATION guarantee — v0.5.2 promised
# "nothing changes for the 583 boxes in the yard until someone claims them" —
# and a box created afterwards has no v0.4.x behaviour to preserve.
#
# Claiming here costs NO network call, which is what keeps `boxyard new`
# offline. `claim_box` reads the remote back because two machines can claim the
# same EXISTING box at once; a box created a moment ago cannot be contested,
# because no other machine knows its id yet.

# %%
#|default_exp integration.cmds.test_new_box_claims

# %%
#|export
import tomllib

import pytest
import tomli_w

from boxyard import const
from boxyard.cmds import new_box
from boxyard._models import get_boxyard_meta
from boxyard.config import get_config


def _boxmeta(config, index_name):
    return tomllib.loads(
        (
            config.local_store_path
            / config.default_storage_location
            / index_name
            / const.BOX_METAFILE_REL_PATH
        ).read_text()
    )


def _drop_machine_name(config_path):
    cfg = tomllib.loads(config_path.read_text())
    cfg.pop("machine_name", None)
    config_path.write_text(tomli_w.dumps(cfg))

# %% [markdown]
# ## By default the creating machine owns it

# %%
#|export
@pytest.mark.integration
def test_new_box_claims_for_this_machine(temp_boxyard):
    _, _, _, config_path, _ = temp_boxyard
    index_name = new_box(config_path=config_path, box_name="mine")

    config = get_config(config_path)
    assert config.machine_name, "the fixture must set a machine name or this proves nothing"
    assert _boxmeta(config, index_name)["write_owner"] == config.machine_name
    assert get_boxyard_meta(config, force_create=True).by_index_name[
        index_name
    ].write_owner == config.machine_name


@pytest.mark.integration
def test_no_claim_leaves_it_unowned(temp_boxyard):
    """For a box created here that will be worked on elsewhere."""
    _, _, _, config_path, _ = temp_boxyard
    index_name = new_box(config_path=config_path, box_name="theirs", claim=False)

    config = get_config(config_path)
    # ABSENT, not empty: an unowned box omits the key entirely, which is what
    # keeps every pre-0.5 boxmeta byte-identical.
    assert "write_owner" not in _boxmeta(config, index_name)

# %% [markdown]
# ## No machine name is not a reason to refuse a box

# %%
#|export
@pytest.mark.integration
def test_without_a_machine_name_the_box_is_still_created(temp_boxyard, monkeypatch, capsys):
    _, _, _, config_path, _ = temp_boxyard
    _drop_machine_name(config_path)
    monkeypatch.delenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, raising=False)

    index_name = new_box(config_path=config_path, box_name="nameless")

    config = get_config(config_path)
    assert config.machine_name is None
    # Created, not refused. A machine with no `machine_name` is an expected
    # state — the myrig template deliberately emits no key for a machine that
    # is not in its machines list — and refusing to create a box over an
    # ownership setting would be wildly out of proportion.
    assert index_name in get_boxyard_meta(config, force_create=True).by_index_name
    assert "write_owner" not in _boxmeta(config, index_name)
    # But NOT silent — and on STDERR, because `boxyard new` prints the index
    # name on stdout and callers parse it.
    captured = capsys.readouterr()
    assert "no `machine_name`" in captured.err
    assert "machine_name" not in captured.out, f"notice polluted stdout: {captured.out!r}"


@pytest.mark.integration
def test_the_env_override_is_enough_to_claim(temp_boxyard, monkeypatch):
    """BOXYARD_MACHINE_NAME already exists, so `new` needs no flag of its own."""
    _, _, _, config_path, _ = temp_boxyard
    _drop_machine_name(config_path)
    monkeypatch.setenv(const.ENV_VAR_BOXYARD_MACHINE_NAME, "from-the-env")

    index_name = new_box(config_path=config_path, box_name="env-owned")
    assert _boxmeta(get_config(config_path), index_name)["write_owner"] == "from-the-env"

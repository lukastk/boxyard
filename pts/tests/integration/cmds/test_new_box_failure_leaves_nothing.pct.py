# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # A Failed `boxyard new` Leaves Nothing Behind
#
# `--group` and `--parent` used to be applied AFTER `new_box` returned, outside
# its try/except and `_rollback_new_box`. A bad group name or a missing parent
# exited 1 with the box already fully created and registered — so a caller that
# reads a non-zero exit as "nothing happened" accumulated orphans.
#
# That is how it surfaced: a cockpit command built its `-g` list from another
# command's output, one entry turned out to be an error string rather than a
# group name, and the failed run left a real box on the machine.
#
# Both are validated up front now — the group charset is a pure string check
# and the parent either exists or does not — which is cheaper than extending
# the rollback and also stops the index name being echoed for a box the command
# then fails on.

# %%
#|default_exp integration.cmds.test_new_box_failure_leaves_nothing

# %%
#|export
import pytest
from typer.testing import CliRunner

from boxyard._cli.app import app
from boxyard._models import get_boxyard_meta
from boxyard.config import get_config


def _yard_contents(config):
    # The directory may not exist yet: the fixture creates it lazily, and a
    # command that creates nothing does not create it either.
    if not config.user_boxes_path.is_dir():
        return []
    return sorted(p.name for p in config.user_boxes_path.iterdir())

# %% [markdown]
# ## A bad group creates nothing

# %%
#|export
@pytest.mark.integration
def test_an_invalid_group_creates_no_box(temp_boxyard):
    _, _, _, config_path, _ = temp_boxyard
    config = get_config(config_path)
    before = _yard_contents(config)

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "new", "-n", "zz-badgroup",
         "-g", "not a valid group", "--no-refresh-user-symlinks"],
    )

    assert result.exit_code != 0
    assert _yard_contents(config) == before, "a failed creation left a box behind"
    assert "zz-badgroup" not in get_boxyard_meta(config, force_create=True).by_index_name
    # And no index name echoed for a box that does not exist.
    assert "zz-badgroup" not in result.stdout, f"stdout: {result.stdout!r}"


@pytest.mark.integration
def test_a_missing_parent_creates_no_box(temp_boxyard):
    _, _, _, config_path, _ = temp_boxyard
    config = get_config(config_path)
    before = _yard_contents(config)

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "new", "-n", "zz-badparent",
         "--parent", "no-such-parent", "--no-refresh-user-symlinks"],
    )

    assert result.exit_code != 0
    assert _yard_contents(config) == before, "a failed creation left a box behind"
    assert "zz-badparent" not in result.stdout

# %% [markdown]
# ## The happy paths still work
#
# Validating up front must not break the thing being validated.

# %%
#|export
@pytest.mark.integration
def test_a_good_group_and_parent_still_apply(temp_boxyard):
    _, _, _, config_path, _ = temp_boxyard

    runner = CliRunner()
    parent = runner.invoke(
        app, ["--config", str(config_path), "new", "-n", "zz-parent",
              "--no-refresh-user-symlinks"]
    ).stdout.strip().split("\n")[-1]

    result = runner.invoke(
        app,
        ["--config", str(config_path), "new", "-n", "zz-child",
         "-g", "a-real-group", "--parent", parent, "--no-refresh-user-symlinks"],
    )
    assert result.exit_code == 0, result.stdout

    meta = get_boxyard_meta(get_config(config_path), force_create=True)
    child = meta.by_index_name[result.stdout.strip().split("\n")[-1]]
    assert "a-real-group" in child.groups
    assert child.parents == [meta.by_index_name[parent].box_id]

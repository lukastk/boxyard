# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `boxyard new --parent` Accepts All Three Forms It Documents
#
# The flag's help says *"Parent box (index name, id, or name)"*, and it only
# ever honoured the **name**: the value was passed as `box_name`, which matches
# against `box_meta.name`, and an index name is never a substring of the bare
# name it ends with. So `--parent 20260601_ab12cd__thing` reported "Box not
# found."

# %%
#|default_exp integration.cmds.test_new_parent_forms

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import tomli_w

from boxyard._models import get_boxyard_meta
from boxyard.cmds import init_boxyard, new_box
from boxyard.config import get_config


def _yard(tmp_path: Path) -> Path:
    config_path = tmp_path / ".config" / "boxyard" / "config.toml"
    init_boxyard(config_path=config_path, data_path=tmp_path / ".boxyard", verbose=False)
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    data["user_boxes_path"] = (tmp_path / "user_boxes").as_posix()
    data["user_box_groups_path"] = (tmp_path / "user_box_groups").as_posix()
    config_path.write_text(tomli_w.dumps(data))
    return config_path


def _boxyard_cli() -> str:
    """
    The console script that belongs to THIS interpreter.

    Resolving it from `sys.executable` rather than from PATH is what stops the
    test silently exercising whatever `boxyard` happens to be installed on the
    machine — which, on a developer box, is a different version.
    """
    cli = Path(sys.executable).parent / "boxyard"
    return str(cli) if cli.exists() else "boxyard"


def _run_new(config_path: Path, name: str, parent: str) -> str:
    """argv, never a shell string — the paths here are temp dirs, but the rule
    is the rule."""
    res = subprocess.run(
        [_boxyard_cli(), "--config", str(config_path), "new",
         "-n", name, "--parent", parent, "--no-initialise-git"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, f"exit {res.returncode}\nstdout: {res.stdout}\nstderr: {res.stderr}"
    return res.stdout.strip().splitlines()[0]

# %% [markdown]
# ## A parent named by index name, by id, and by name

# %%
#|export
@pytest.mark.integration
@pytest.mark.parametrize("form", ["index_name", "box_id", "name"])
def test_new_parent_accepts_every_documented_form(tmp_path, form):
    config_path = _yard(tmp_path)
    parent_index = new_box(
        config_path=config_path, box_name="the-parent", initialise_git=False
    )
    config = get_config(config_path)
    parent_meta = get_boxyard_meta(config, force_create=True).by_index_name[parent_index]

    selector = {
        "index_name": parent_index,
        "box_id": parent_meta.box_id,
        "name": "the-parent",
    }[form]

    # TESTREF: test_new_parent_accepts_every_documented_form
    child_index = _run_new(config_path, f"child-{form}", selector)

    config = get_config(config_path)
    child = get_boxyard_meta(config, force_create=True).by_index_name[child_index]
    assert child.parents == [parent_meta.box_id], (
        f"--parent {form} did not attach the parent: {child.parents}"
    )

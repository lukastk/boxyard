# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Two `new_box` Paths That Had Never Run
#
# Both were invisible because nothing reached them: `sync_before_new_box`
# defaults to False, and `git init` does not fail on a developer machine.
#
# 1. **`sync_before_new_box`** — the branch imported `sync_boxmetas`, a name
#    `boxyard.cmds` has never exported (the function is
#    `sync_missing_boxmetas`), and then drove it through
#    `asyncio.get_event_loop().run_until_complete(...)`, which raises
#    `RuntimeError: There is no current event loop` on Python 3.14. Turning the
#    setting on did not sync boxmetas — it made `boxyard new` fail outright.
#
# 2. **A failing `git init`** — the code has always carried a "Warning: Failed
#    to initialise git box" branch, but `check=True` raised first, so the
#    warning was unreachable and the failure instead rolled the whole box back.
#    A box is complete before `git init` runs; losing it over an optional
#    convenience (and, with `--from`, unwinding a directory move) is the wrong
#    trade.

# %%
#|default_exp integration.cmds.test_new_box_sync_and_git

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import os
import tomllib
from pathlib import Path

import pytest
import tomli_w

from boxyard._models import BoxPart, get_boxyard_meta
from boxyard.cmds import new_box
from boxyard.config import get_config

from tests.integration.conftest import create_boxyards


def _set_config_key(config_path: Path, key: str, value) -> None:
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    data[key] = value
    config_path.write_text(tomli_w.dumps(data))

# %% [markdown]
# ## `sync_before_new_box = true` creates a box instead of raising

# %%
#|export
@pytest.mark.integration
def test_new_box_with_sync_before_new_box():
    remote_name, _remote_path, _config, config_path, _data_path = create_boxyards()
    _set_config_key(config_path, "sync_before_new_box", True)

    # TESTREF: test_new_box_sync_before_new_box
    index_name = new_box(
        config_path=config_path,
        box_name="synced-first",
        storage_location=remote_name,
        initialise_git=False,
    )

    config = get_config(config_path)
    assert config.sync_before_new_box is True
    box_meta = get_boxyard_meta(config, force_create=True).by_index_name[index_name]
    assert box_meta.name == "synced-first"
    assert box_meta.get_local_part_path(config, BoxPart.DATA).exists()

# %% [markdown]
# ## A failing `git init` warns; the box survives

# %%
#|export
@pytest.mark.integration
def test_new_box_survives_failing_git_init(tmp_path, monkeypatch, capsys):
    remote_name, _remote_path, _config, config_path, _data_path = create_boxyards()

    # A `git` that always fails, first on PATH. Stands in for the real reason
    # this branch matters: a machine where git is missing or broken.
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text("#!/bin/sh\necho 'git: simulated failure' >&2\nexit 1\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")

    # TESTREF: test_new_box_survives_failing_git_init
    index_name = new_box(
        config_path=config_path,
        box_name="no-git-here",
        storage_location=remote_name,
        initialise_git=True,
    )

    config = get_config(config_path)
    box_meta = get_boxyard_meta(config, force_create=True).by_index_name[index_name]
    data_path = box_meta.get_local_part_path(config, BoxPart.DATA)
    assert data_path.exists()
    assert not (data_path / ".git").exists()

    # The warning must be loud — the CLI passes verbose=False, so anything
    # gated on verbosity would make a failed `git init` completely silent.
    err = capsys.readouterr().err
    assert "git init" in err
    assert "simulated failure" in err

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `--from` Works Across Filesystems
#
# `new_box` moved the tree in with `Path.rename`, which is `os.rename` and
# cannot cross a filesystem boundary. Staging a folder anywhere not on the same
# filesystem as `user_boxes_path` — `/tmp` where it is tmpfs, `/dev/shm`, an
# external disk — failed with `Invalid cross-device link`.
#
# The unpleasant part was the machine-dependence: the same command worked on
# mymain (same-fs `/tmp`) and failed on ideapad (tmpfs `/tmp`).
#
# `shutil.move` renames when it can and falls back to copy+unlink when it
# cannot, so the fast path is unchanged. `--copy` is not a workaround: it
# leaves the source behind and pays a second full copy, which is the cost
# `--from`'s move exists to avoid.

# %%
#|default_exp integration.cmds.test_new_box_cross_device

# %%
#|export
import os
import shutil
from pathlib import Path

import pytest

from boxyard.cmds import new_box
from boxyard._models import BoxPart, get_boxyard_meta
from boxyard.config import get_config


def _other_filesystem(target: Path):
    """A directory on a DIFFERENT filesystem from target, or None."""
    for candidate in (Path("/dev/shm"), Path("/run/user") / str(os.getuid())):
        if not candidate.is_dir() or not os.access(candidate, os.W_OK):
            continue
        if os.stat(candidate).st_dev != os.stat(target).st_dev:
            return candidate
    return None

# %% [markdown]
# ## Moving in from another filesystem

# %%
#|export
@pytest.mark.integration
def test_from_across_a_filesystem_boundary(temp_boxyard):
    _, _, _, config_path, _ = temp_boxyard
    config = get_config(config_path)
    config.user_boxes_path.mkdir(parents=True, exist_ok=True)

    other = _other_filesystem(config.user_boxes_path)
    if other is None:
        pytest.skip("no second filesystem available to stage on")

    staged = other / f"boxyard-crossdev-{os.getpid()}"
    (staged / "sub").mkdir(parents=True)
    (staged / "sub" / "payload.txt").write_text("hi\n")
    try:
        index_name = new_box(
            config_path=config_path, box_name="crossdev", from_path=staged
        )

        bm = get_boxyard_meta(get_config(config_path), force_create=True).by_index_name[
            index_name
        ]
        data = bm.get_local_part_path(get_config(config_path), BoxPart.DATA)
        assert (data / "sub" / "payload.txt").read_text() == "hi\n"
        # MOVED, not copied: leaving the source behind is what `--copy` is for,
        # and paying a second full copy is the cost `--from` exists to avoid.
        assert not staged.exists(), "the source survived, so this was a copy"
    finally:
        shutil.rmtree(staged, ignore_errors=True)

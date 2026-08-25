# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `init` Must Link Local Storage Locations
#
# A `local` storage location needs an entry in `local_store` pointing at its
# `store_path`; without it the location is configured but unusable.
#
# `init` never created it. The guard read
# `storage_type != StorageType.LOCAL.value`, and `StorageType` is a plain
# `Enum` rather than a `str, Enum` — so `StorageType.LOCAL == "local"` is
# **False**, the comparison was ALWAYS true, and the loop `continue`d every
# time. Silently: `init` printed "Done!" and the link simply was not there.
#
# Verified on the live yard before fixing — `~/.boxyard/local_store/` held only
# the rclone location, and the configured `local` one had never been created.

# %%
#|default_exp unit.cmds.test_init_local_storage

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import tomli_w
from pathlib import Path

from boxyard.cmds import init_boxyard
from boxyard.config import get_config


def _write_config(tmp_path: Path) -> Path:
    """A config with BOTH a local and an rclone storage location."""
    cfg_path = tmp_path / "config" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(tomli_w.dumps({
        "default_storage_location": "mylocal",
        "boxyard_data_path": (tmp_path / "data").as_posix(),
        "box_timestamp_format": "date_only",
        "user_boxes_path": (tmp_path / "boxes").as_posix(),
        "user_box_groups_path": (tmp_path / "groups").as_posix(),
        "default_box_groups": [],
        "box_subid_character_set": "abcdefghijklmnopqrstuvwxyz0123456789",
        "box_subid_length": 6,
        "max_concurrent_rclone_ops": 2,
        "box_groups": {},
        "virtual_box_groups": {},
        "storage_locations": {
            "mylocal": {
                "storage_type": "local",
                "store_path": (tmp_path / "mylocal_store").as_posix(),
            },
            "myremote": {"storage_type": "rclone", "store_path": "boxyard"},
        },
    }))
    return cfg_path

# %%
#|export
def test_init_links_local_storage_locations(tmp_path):
    """A `local` storage location gets a link in local_store pointing at its store."""
    cfg_path = _write_config(tmp_path)
    init_boxyard(config_path=cfg_path, data_path=tmp_path / "data", verbose=False)

    config = get_config(cfg_path)
    link = config.local_store_path / "mylocal"

    assert link.exists(), (
        "init did not create the local_store entry for a `local` storage "
        "location — the location is configured but unusable"
    )
    assert link.is_symlink(), f"{link} exists but is not a symlink"
    assert link.resolve() == (tmp_path / "mylocal_store").resolve(), (
        f"the link points at {link.resolve()}, not the configured store_path"
    )
    assert (tmp_path / "mylocal_store").is_dir(), (
        "init did not create the local storage location's store_path"
    )

# %%
#|export
def test_init_does_not_link_rclone_storage_locations(tmp_path):
    """An rclone location must NOT be linked — its store lives on the remote.

    The other half: a fix that linked everything would create a local directory
    shadowing a remote store.
    """
    cfg_path = _write_config(tmp_path)
    init_boxyard(config_path=cfg_path, data_path=tmp_path / "data", verbose=False)

    config = get_config(cfg_path)
    assert not (config.local_store_path / "myremote").is_symlink(), (
        "init linked an rclone storage location, which has no local store"
    )

# %%
#|export
def test_init_is_idempotent(tmp_path):
    """Running init twice must not fail on the existing link."""
    cfg_path = _write_config(tmp_path)
    init_boxyard(config_path=cfg_path, data_path=tmp_path / "data", verbose=False)
    init_boxyard(config_path=cfg_path, data_path=tmp_path / "data", verbose=False)

    config = get_config(cfg_path)
    assert (config.local_store_path / "mylocal").is_symlink()

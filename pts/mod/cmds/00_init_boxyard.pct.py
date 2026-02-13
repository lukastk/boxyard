# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _init_boxyard

# %%
#|default_exp cmds._init_boxyard
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path

from boxyard import const

# %%
#|set_func_signature
def init_boxyard(
    config_path: Path | None = None,
    data_path: Path | None = None,
    verbose: bool = False,
):
    """
    Initialize boxyard.

    Will create the necessary folders and files to start using boxyard.
    """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_boxyards

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %%
# Args
config_path = config_path
data_path = data_path
verbose = True

# %% [markdown]
# # Function body

# %%
#|export
config_path = config_path or const.DEFAULT_CONFIG_PATH
data_path = data_path or const.DEFAULT_DATA_PATH

if (
    config_path.expanduser().as_posix()
    != const.DEFAULT_CONFIG_PATH.expanduser().as_posix()
):
    if verbose:
        print(
            f"Using a non-default config path. Please set the environment variable {const.ENV_VAR_BOXYARD_CONFIG_PATH} to the given config path for boxyard to use it. "
        )

# %% [markdown]
# Create a default config file if it doesn't exist

# %%
#|export
from boxyard.config import get_config, _get_default_config_dict, Config
import toml

if not config_path.expanduser().exists():
    if verbose:
        print("Creating config file at:", config_path)
    Path(config_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    default_config_dict = _get_default_config_dict(
        config_path=config_path, data_path=data_path
    )
    del default_config_dict[
        "config_path"
    ]  # Don't save the config path to the config file
    config_toml = toml.dumps(default_config_dict)

    Path(config_path).expanduser().write_text(config_toml)
config = get_config(config_path)

# %% [markdown]
# Create the default `.rclone_exclude` file

# %%
#|export
if not config.default_rclone_exclude_path.exists():
    config.default_rclone_exclude_path.write_text(const.DEFAULT_RCLONE_EXCLUDE)

# %% [markdown]
# For testing purposes, modify the config

# %%
config = Config(
    **{
        "config_path": config_path,
        **config.model_dump(),
        "storage_locations": {
            "fake": {
                "storage_type": "rclone",
                "store_path": data_path / "fake_store",
            }
        },
    }
)

# %% [markdown]
# Create folders

# %%
#|export
paths = [
    config.boxyard_data_path,
    config.local_store_path,
]

for path in paths:
    if not path.exists():
        if verbose:
            print(f"Creating folder: {path}")
        path.mkdir(parents=True, exist_ok=True)

# %% [markdown]
# Set up symlinks for every storage location of type `local`

# %%
#|export
from boxyard.config import StorageType

for storage_location_name, storage_location in config.storage_locations.items():
    if storage_location.storage_type != StorageType.LOCAL.value:
        continue
    storage_location.store_path.mkdir(parents=True, exist_ok=True)
    if (config.local_store_path / storage_location_name).exists():
        (config.local_store_path / storage_location_name).unlink()
    (config.local_store_path / storage_location_name).symlink_to(
        storage_location.store_path
    )

# %% [markdown]
# Create `boxyard_rclone.conf` if it doesn't exist

# %%
#|export
from boxyard.config import _default_rclone_config

if not config.rclone_config_path.exists():
    if verbose:
        print(f"Creating rclone config file at: {config.rclone_config_path}")
    config.rclone_config_path.write_text(_default_rclone_config)

# %% [markdown]
# Done

# %%
#|export
if verbose:
    print("Done!\n")
    print("You can modify the config at:", config_path)
    print("All boxyard data is stored in:", config.boxyard_data_path)

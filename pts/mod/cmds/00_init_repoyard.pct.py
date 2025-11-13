# %% [markdown]
# # _init_repoyard

# %%
#|default_exp cmds._init_repoyard
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path

from repoyard import const


# %%
#|set_func_signature
def init_repoyard(
    config_path: Path|None = None,
    data_path: Path|None = None,
):
    """
    Initialize repoyard.
    
    Will create the necessary folders and files to start using repoyard.
    """
    ...


# %% [markdown]
# Set up testing args

# %%
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = tests_working_dir / "_cmds" / "init_repoyard"
# !rm -rf {test_folder_path}

# %%
# Args
config_path = test_folder_path / "repoyard_config" / "config.toml"
data_path = test_folder_path / "repoyard_data"

# %% [markdown]
# # Function body

# %%
#|export
config_path = config_path or const.DEFAULT_CONFIG_PATH
data_path = data_path or const.DEFAULT_DATA_PATH

if config_path.as_posix() != const.DEFAULT_CONFIG_PATH.as_posix():
    print(f"Using a non-default config path. Please set the environment variable {const.ENV_VAR_REPOYARD_CONFIG_PATH} to the given config path for repoyard to use it. ")

# %% [markdown]
# Create a default config file if it doesn't exist

# %%
#|export
from repoyard.config import get_config, _get_default_config_dict, Config
import toml
if not config_path.exists():
    print("Creating config file at:", config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    default_config_dict = _get_default_config_dict(config_path=config_path, data_path=data_path)
    del default_config_dict['config_path'] # Don't save the config path to the config file
    config_toml = toml.dumps(default_config_dict)
    Path(config_path).write_text(config_toml)
config = get_config(config_path)

# %% [markdown]
# Create the default `.repoyard_exclude` file

# %%
#|export
if not config.default_repoyard_exclude_path.exists():
    config.default_repoyard_exclude_path.write_text(const.DEFAULT_REPOYARD_EXCLUDE)

# %% [markdown]
# For testing purposes, modify the config

# %%
config = Config(**{
    'config_path' : config_path,
    **config.model_dump(),
    'storage_locations' : {
        'fake' : {
            'storage_type' : 'rclone',
            'store_path' : data_path / "fake_store",
        }
    }
})

# %% [markdown]
# Create folders

# %%
#|export
paths = [
    config.repoyard_data_path,
    config.local_store_path,
]

for path in paths:
    if not path.exists():
        print(f"Creating folder: {path}")
        path.mkdir(parents=True, exist_ok=True)

# %% [markdown]
# Set up symlinks for every storage location of type `local`

# %%
#|export
for storage_location_name, storage_location in config.storage_locations.items():
    storage_location.store_path.mkdir(parents=True, exist_ok=True)
    if (config.local_store_path / storage_location_name).exists():
        (config.local_store_path / storage_location_name).unlink()
    (config.local_store_path / storage_location_name).symlink_to(storage_location.store_path)

# %% [markdown]
# Create `repoyard_rclone.conf` if it doesn't exist

# %%
#|export
from repoyard.config import _default_rclone_config
if not config.rclone_config_path.exists():
    print(f"Creating rclone config file at: {config.rclone_config_path}")
    config.rclone_config_path.write_text(_default_rclone_config)

# %% [markdown]
# Create a rclone destination locally to test with

# %%
rclone_local_test_path = test_folder_path / 'rclone_local_test'
rclone_local_test_path.mkdir(parents=True, exist_ok=True)
config.rclone_config_path.write_text(f"""
[test_local]
type = alias
remote = {rclone_local_test_path}
""");

# %% [markdown]
# Done

# %%
#|export
print("Done!\n")
print("You can modify the config at:", config_path)
print("All repoyard data is stored in:", config.repoyard_data_path)

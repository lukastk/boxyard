# %% [markdown]
# # init

# %%
#|default_exp _cmds.init
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import repoyard._cmds.init as this_module

# %%
#|top_export
from pathlib import Path

from repoyard._cli import app
import typer
from typer import Argument, Option
from typing_extensions import Annotated

from repoyard import const


# %%
#|set_func_signature
@app.command(name='init')
def init(
    config_path: Path = const.DEFAULT_CONFIG_PATH,
):
    """
    Initialize repoyard.
    
    Will create the necessary folders and files to start using repoyard.
    """
    ...


# %%
test_folder_path = const.pkg_path.parent / "test_folder"
# !rm -rf {test_folder_path}

config_path = test_folder_path / "repoyard_config" / "config.toml"

# %%
#|export
if config_path.as_posix() != const.DEFAULT_CONFIG_PATH.as_posix():
    print(f"Using a non-default config path. Please set the environment variable {const.ENV_VAR_REPOYARD_CONFIG_PATH} to the given config path for repoyard to use it. ")

# %% [markdown]
# Create a default config file if it doesn't exist

# %%
#|export
from repoyard.config import _save_config, get_config, _get_default_config, Config
if not config_path.exists():
    print("Creating config file at:", config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    default_config = _get_default_config(config_path=config_path)
    _save_config(default_config, config_path)
    
config = get_config(config_path)

# %% [markdown]
# For testing purposes, modify the config

# %%
config = Config(**{
    'config_path' : config_path,
    **{k:v for k,v in config.model_dump().items() if k != 'config_path'},
    'repoyard_data_path' : test_folder_path / "repoyard_data",
    'user_repos_path' : test_folder_path / "user_repos",
    'user_repo_groups_path' : test_folder_path / "user_repo_groups",
})
_save_config(config, config_path)

# %% [markdown]
# Create folders

# %%
#|export
paths = [
    config.repoyard_data_path,
    config.user_repos_path,
    config.user_repo_groups_path,
    config.synced_repostore_path,
    config.synced_repometa_path,
    config.local_storage_path,
    config.local_repometa_path,
    config.local_repostore_path,
]

for path in paths:
    if not path.exists():
        print(f"Creating folder: {path}")
        path.mkdir(parents=True, exist_ok=True)

# %% [markdown]
# Create `repoyard_rclone.conf` if it doesn't exist

# %%
#|export
from repoyard.config import _default_rclone_config
if not config.rclone_config_path.exists():
    print(f"Creating rclone config file at: {config.rclone_config_path}")
    config.rclone_config_path.write_text(_default_rclone_config)

# %% [markdown]
# Done

# %%
#|export
print("Done!\n")
print("You can modify the config at:", config_path)
print("All repoyard data is stored in:", config.repoyard_data_path)

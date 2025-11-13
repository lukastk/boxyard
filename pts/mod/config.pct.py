# %% [markdown]
# # config

# %%
#|default_exp config

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import repoyard.config as this_module

# %%
#|export
from pydantic import BaseModel, Field, model_validator
from typing import Literal
import os
from pathlib import Path
import toml, json

from repoyard import const


# %% [markdown]
# # `config.json`

# %%

#|export
class StorageConfig(const.StrictModel):
    storage_type: Literal["rclone", "local"]
    repometa_path: Path
    repostore_path: Path
    
    @model_validator(mode='after')
    def validate_config(self):
        # Expand paths
        self.repometa_path = self.repometa_path.expanduser()
        self.repostore_path = self.repostore_path.expanduser()
        return self
    
class SyncingConfig(const.StrictModel):
    bisync_wait_time: int
    
class RepoGroupConfig(const.StrictModel):
    group_name: str
    is_virtual: bool = False
    repo_title_mode: Literal["full_name", "datetime", "name"] = "datetime"
    unique_repo_names: bool = False

class Config(const.StrictModel):
    config_path : Path # Path to the config file. Will not be saved to the config file.
    
    default_storage_location : str
    repoyard_data_path : Path
    user_repos_path : Path
    user_repo_groups_path : Path
    storage_locations : dict[str, StorageConfig]
    syncing : SyncingConfig
    repo_groups : list[RepoGroupConfig]
    
    @property
    def log_path(self) -> Path:
        return self.repoyard_data_path / "repoyard.log"
    
    @property
    def included_repostore_path(self) -> Path:
        return self.repoyard_data_path / "included_repostore"
    
    @property
    def synced_repometa_path(self) -> Path:
        return self.repoyard_data_path / "synced_repometa"
    
    @property
    def repoyard_meta_path(self) -> Path:
        return self.repoyard_data_path / "repoyard_meta.json"
    
    @property
    def sync_settings_path(self) -> Path:
        return self.repoyard_data_path / "sync_settings.toml"
    
    @property
    def rclone_config_path(self) -> Path:
        return Path(self.config_path).parent / "repoyard_rclone.conf"
    
    @property
    def local_repometa_path(self) -> Path:
        return self.repoyard_data_path / const.LOCAL_REPOMETA_REL_PATH
    
    @property
    def local_repostore_path(self) -> Path:
        return self.repoyard_data_path / const.LOCAL_REPOSTORE_REL_PATH
    
    @property
    def default_repoyard_ignore_path(self) -> str:
        return self.config_path.parent / "default.repoyard_ignore"
    
    @model_validator(mode='after')
    def validate_config(self):
        # Expand all paths
        self.repoyard_data_path = Path(self.repoyard_data_path).expanduser()
        self.user_repos_path = Path(self.user_repos_path).expanduser()
        self.user_repo_groups_path = Path(self.user_repo_groups_path).expanduser()
        
        import re
        for name in self.storage_locations.keys():
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
                raise ValueError(f"StorageConfig name {name} is invalid. StorageConfig names can only contain alphanumeric characters, underscore(_), or dash(-).")
        
        if len(self.storage_locations) == 0:
            raise ValueError("No storage locations defined.")
            
        # Check that the default storage location exists
        if not any(name == self.default_storage_location for name in self.storage_locations):
            raise ValueError(f"default_storage_location '{self.default_storage_location}' not found in storage_locations")
        
        return self


# %%
#|export
def get_config(path: Path|None = None) -> Config:
    if path is None: path = const.DEFAULT_CONFIG_PATH
    return Config(**{
        'config_path' : path,
        **toml.load(path)
    })


# %%
#|export
def _get_default_config_dict(config_path=None, data_path=None) -> Config:
    if config_path is None: config_path = const.DEFAULT_CONFIG_PATH
    if data_path is None: data_path = const.DEFAULT_DATA_PATH
    config_path = Path(config_path)
    data_path = Path(data_path)
    
    
    config_dict = dict(
        config_path=config_path.as_posix(),
        default_storage_location = "local",
        repoyard_data_path = data_path.as_posix(),
        user_repos_path = const.DEFAULT_USER_REPOS_PATH.as_posix(),
        user_repo_groups_path = const.DEFAULT_USER_REPO_GROUPS_PATH.as_posix(),
        storage_locations = {
            "local": dict(
                storage_type="local",
                repometa_path=(data_path / const.LOCAL_REPOMETA_REL_PATH).as_posix(),
                repostore_path=(data_path / const.LOCAL_REPOSTORE_REL_PATH).as_posix(),
            )
        },
        syncing = {
            "bisync_wait_time": 10,
        },
        repo_groups = [],
    )
    return config_dict

# %% [markdown]
# # `rclone.conf`

# %%
#|exporti
_default_rclone_config = """
"""

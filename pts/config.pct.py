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
from pydantic import BaseModel, Field
from typing import Literal
import os
from pathlib import Path
import toml, json

from repoyard import const


# %% [markdown]
# # `config.json`

# %%
#|export
class StorageConfig(BaseModel):
    alias: str
    storage_type: Literal["rclone", "local"]
    repometa_path: Path
    repostore_path: Path
    
class SyncingConfig(BaseModel):
    bisync_wait_time: int = 10

class Config(BaseModel):
    config_path : Path # Path to the config file. Will not be saved to the config file.
    
    repoyard_data_path : Path = Field(default_factory=lambda: Path.home() / ".repoyard")
    user_repos_path : Path = Field(default_factory=lambda: Path.home() / "repos")
    user_repo_groups_path : Path = Field(default_factory=lambda: Path.home() / "repo_groups")
    storage_locations : list[StorageConfig] = Field(default_factory=list)
    syncing : SyncingConfig = Field(default_factory=SyncingConfig)
    
    @property
    def log_path(self) -> Path:
        return self.repoyard_data_path / "repoyard.log"
    
    @property
    def synced_repostore_path(self) -> Path:
        return self.repoyard_data_path / "synced" / "repostore"
    
    @property
    def synced_repometa_path(self) -> Path:
        return self.repoyard_data_path / "synced" / "repometa"
    
    @property
    def sync_settings_path(self) -> Path:
        return self.repoyard_data_path / "sync_settings.toml"
    
    @property
    def rclone_config_path(self) -> Path:
        return self.repoyard_data_path / "repoyard_rclone.conf"
    
    @property
    def local_storage_path(self) -> Path:
        return self.repoyard_data_path / "local"
    
    @property
    def local_repometa_path(self) -> Path:
        return self.local_storage_path / "repometa"
    
    @property
    def local_repostore_path(self) -> Path:
        return self.local_storage_path / "repostore"


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
def _get_default_config(config_path: Path|None = None) -> Config:
    if config_path is None: config_path = const.DEFAULT_CONFIG_PATH
    return Config(
        config_path = config_path,
    )


# %%
#|export
def _save_config(config: Config, save_path: Path):
    config_dump = json.loads(config.model_dump_json()) # Safer using toml.dumps directly on default_config.model_dump()
    del config_dump['config_path'] # Don't save the config path to the config file
    config_toml = toml.dumps(config_dump)
    Path(save_path).write_text(config_toml)


# %% [markdown]
# # `rclone.conf`

# %%
#|exporti
_default_rclone_config = """
"""

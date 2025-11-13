# %% [markdown]
# # _repos

# %%
#|default_exp _repos

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import repoyard._repos as this_module

# %%
#|export
from typing import Callable
from pydantic import BaseModel, Field
from pathlib import Path
import toml, json
from datetime import datetime, timezone
from ulid import ULID
import repoyard.config
from repoyard import const
from repoyard.config import RepoGroupConfig


# %%
#|export
class RepoMeta(const.StrictModel):
    ulid: ULID = Field(default_factory=ULID)
    name: str
    storage_location: str
    creator_hostname: str
    groups: list[str]
    
    @property
    def full_name(self) -> str:
        return f"{str(self.ulid)}__{self.name}"

    def check_included(self, config: repoyard.config.Config) -> bool:
        included_repo_path = (config.included_repostore_path / self.full_name)
        is_included = included_repo_path.is_dir() and included_repo_path.exists()
        return is_included
    
    def save(self, config: repoyard.config.Config):
        path = self.get_synced_repometa_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        model_dump = self.model_dump()
        del model_dump['ulid']
        del model_dump['name']
        path.write_text(toml.dumps(model_dump))
        
    def get_included_repo_path(self, config: repoyard.config.Config) -> Path:
        return config.included_repostore_path / self.full_name
    
    def get_remote_repo_path(self, config: repoyard.config.Config) -> Path:
        return config.storage_locations[self.storage_location].repostore_path / self.full_name
    
    def get_synced_repometa_path(self, config: repoyard.config.Config) -> Path:
        return config.synced_repometa_path / self.storage_location / f"{self.full_name}.toml"
    
    def get_remote_repometa_path(self, config: repoyard.config.Config) -> Path:
        return config.storage_locations[self.storage_location].repometa_path / f"{self.full_name}.toml"


# %%
#|export
class RepoyardMeta(BaseModel):
    by_full_name: dict[str, RepoMeta]
    by_ulid: dict[str, RepoMeta]


# %%
#|export
def create_repoyard_meta(
    config: repoyard.config.Config
) -> RepoyardMeta:
    """Create a dict of all repo metas. To be saved in `config.repoyard_meta_path`."""
    by_full_name = {}
    by_ulid = {}
    for storage_location_name in config.storage_locations:
        path = config.synced_repometa_path / storage_location_name
        for repo_meta_path in path.glob('*.toml'):
            full_name = repo_meta_path.stem
            ulid, name = full_name.split('__', 1)
            repo_meta = RepoMeta(**{
                **toml.loads(repo_meta_path.read_text()),
                'ulid': ulid,
                'name': name,
                'storage_location': storage_location_name,
            })
            by_full_name[full_name] = repo_meta
            by_ulid[str(repo_meta.ulid)] = repo_meta
    return RepoyardMeta(by_full_name=by_full_name, by_ulid=by_ulid)



# %%
#|export
def get_repoyard_meta(
    config: repoyard.config.Config,
    force_create: bool=False,
) -> RepoyardMeta:
    if not config.repoyard_meta_path.exists() or force_create:
        repoyard_meta = create_repoyard_meta(config)
        config.repoyard_meta_path.write_text(repoyard_meta.model_dump_json())
    return RepoyardMeta.model_validate_json(config.repoyard_meta_path.read_text())


# %%
#|export
def get_virtual_repo_group_filters(
    config: repoyard.config.Config,
) -> dict[str, Callable[[RepoMeta], bool]]:
    return {}


# %%
#|export
def get_repo_group_configs(
    config: repoyard.config.Config,
    repo_metas: list[RepoMeta],
) -> dict[str, RepoGroupConfig]:
    repo_group_configs = {rgc.group_name for rgc in config.repo_groups}
    for repo_meta in repo_metas.values():
        if not repo_meta.is_included: continue
        for group_name in repo_meta.groups:
            if group_name not in repo_group_configs:
                repo_group_configs[group_name] = RepoGroupConfig(group_name=group_name)
    return repo_group_configs


# %%
#|export
def create_user_repos_symlinks(
    config: repoyard.config.Config,
    repo_metas: list[RepoMeta],
):
    for path in config.user_repos_path.glob('*'):
        if path.is_symlink(): path.unlink()
    
    for repo_meta in repo_metas.values():
        if not repo_meta.is_included: continue
        source_path = config.included_repostore_path / repo_meta.full_name
        symlink_path = config.user_repos_path / repo_meta.full_name
        if symlink_path.is_symlink(): 
            if symlink_path.resolve() != source_path.resolve():
                symlink_path.unlink()
            else: continue # already correct
        symlink_path.symlink_to(source_path, target_is_directory=True)


# %%
#|export
def create_user_repo_group_symlinks(
    config: repoyard.config.Config,
    repo_metas: list[RepoMeta],
):
    virtual_repo_groups = get_virtual_repo_group_filters(config)
    groups = get_repo_group_configs(config, repo_metas)
    
    symlink_paths = []
    
    for group_name, group_config in groups.items():
        for repo_meta in repo_metas.values():
            if not repo_meta.is_included: continue
            is_in_group = False
            if group_config.is_virtual:
                is_in_group = virtual_repo_groups[group_name](repo_meta)
            else:
                is_in_group = group_name in repo_meta.groups
            if is_in_group:
                source_path = config.included_repostore_path / repo_meta.full_name
                symlink_path = config.user_repo_groups_path / group_name / repo_meta.full_name
                symlink_path.parent.mkdir(parents=True, exist_ok=True)
                symlink_paths.append(symlink_path.resolve().as_posix())
                if symlink_path.is_symlink():
                    if symlink_path.resolve() != source_path.resolve():
                        symlink_path.unlink()
                    else: continue # already correct
                symlink_path.symlink_to(source_path, target_is_directory=True)
    
    # Remove any symlinks that are not in the symlink_paths list
    for group_folder_path in config.user_repo_groups_path.glob('*'):
        for symlink_path in group_folder_path.glob('*'):
            if not symlink_path.is_symlink(): continue
            if symlink_path.resolve().as_posix() not in symlink_paths:
                symlink_path.unlink()
        # Remove the group folder if it is empty after removing symlinks
        if not any(group_folder_path.iterdir()):
            group_folder_path.rmdir()

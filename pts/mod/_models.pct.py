# %% [markdown]
# # _models

# %%
#|default_exp _models

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import repoyard._models as this_module

# %%
#|export
from typing import Callable, Literal
from pydantic import BaseModel, Field, model_validator
from pathlib import Path
import toml, json
from datetime import datetime, timezone
import random
from ulid import ULID
from enum import Enum
import repoyard.config
from repoyard import const
from repoyard.config import RepoGroupConfig


# %% [markdown]
# # `RepoMeta`

# %%
#|export
class RepoPart(Enum):
    DATA = "data"
    META = "meta"
    CONF = "conf"


# %%
#|exporti
def _create_repo_subid(character_set: str, length: int) -> str:
    return ''.join(random.choices(character_set, k=length))

___repo_timestamp_length = {}
def _get_repo_timestamp_length(config: repoyard.config.Config) -> int:
    if config.config_path not in ___repo_timestamp_length:
        test_repo_meta = RepoMeta.create(config, "test", "test", "test", [])
        ___repo_timestamp_length[config.config_path] = len(test_repo_meta.creation_timestamp_utc)
    return ___repo_timestamp_length[config.config_path]


# %%
#|export
class RepoMeta(const.StrictModel):
    creation_timestamp_utc: str
    repo_subid: str
    name: str
    storage_location: str
    creator_hostname: str
    groups: list[str]

    @classmethod
    def create(
        cls,
        config: repoyard.config.Config,
        name: str,
        storage_location_name: str,
        creator_hostname: str,
        groups: list[str],
        creation_timestamp_utc: datetime|None = None,
    ) -> 'RepoMeta':
        if creation_timestamp_utc is None:
            creation_timestamp_utc = datetime.now(timezone.utc).strftime(const.REPO_TIMESTAMP_FORMAT)
        else:
            creation_timestamp_utc = creation_timestamp_utc.strftime(const.REPO_TIMESTAMP_FORMAT)

        return RepoMeta(
            creation_timestamp_utc=creation_timestamp_utc,
            repo_subid=_create_repo_subid(config.repo_subid_character_set, config.repo_subid_length),
            name=name,
            storage_location=storage_location_name,
            creator_hostname=creator_hostname,
            groups=groups,
        )

    @property
    def creation_timestamp_datetime(self) -> datetime:
        return datetime.strptime(self.creation_timestamp_utc, const.REPO_TIMESTAMP_FORMAT)

    @property
    def repo_id(self) -> str:
        return f"{self.creation_timestamp_utc}_{str(self.repo_subid)}"

    @property
    def full_name(self) -> str:
        return f"{self.repo_id}__{self.name}"

    def get_storage_location_config(self, config: repoyard.config.StorageConfig) -> repoyard.config.StorageConfig:
        return config.storage_locations[self.storage_location]

    def get_remote_path(self, config: repoyard.config.Config) -> Path:
        return config.storage_locations[self.storage_location].store_path / const.REMOTE_REPOS_REL_PATH / self.full_name
    
    def get_local_path(self, config: repoyard.config.Config) -> Path:
        return config.local_store_path / self.storage_location / self.full_name

    def get_remote_part_path(self, config: repoyard.config.Config, repo_part: RepoPart) -> Path:
        if repo_part == RepoPart.DATA:
            return self.get_remote_path(config) / const.REPO_DATA_REL_PATH
        elif repo_part == RepoPart.META:
            return self.get_remote_path(config) / const.REPO_METAFILE_REL_PATH
        elif repo_part == RepoPart.CONF:
            return self.get_remote_path(config) / const.REPO_CONF_REL_PATH
        else:
            raise ValueError(f"Invalid repo part: {repo_part}")

    def get_local_part_path(self, config: repoyard.config.Config, repo_part: RepoPart) -> Path:
        if repo_part == RepoPart.DATA:
            return config.user_repos_path / self.full_name
        elif repo_part == RepoPart.META:
            return self.get_local_path(config) / const.REPO_METAFILE_REL_PATH
        elif repo_part == RepoPart.CONF:
            return self.get_local_path(config) / const.REPO_CONF_REL_PATH
        else:
            raise ValueError(f"Invalid repo part: {repo_part}")

    def get_remote_sync_record_path(self, config: repoyard.config.Config, repo_part: RepoPart) -> Path:
        sl_conf = self.get_storage_location_config(config)
        return sl_conf.store_path / const.SYNC_RECORDS_REL_PATH / self.full_name / f"{repo_part.value}.rec"
    
    def get_local_sync_record_path(self, config: repoyard.config.Config, repo_part: RepoPart) -> Path:
        return config.repoyard_data_path / const.SYNC_RECORDS_REL_PATH / self.full_name / f"{repo_part.value}.rec"
    
    def check_included(self, config: repoyard.config.Config) -> bool:
        included_repo_path = self.get_local_part_path(config, RepoPart.DATA)
        return included_repo_path.is_dir() and included_repo_path.exists()
    
    def save(self, config: repoyard.config.Config):
        save_path = self.get_local_part_path(config, RepoPart.META)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        model_dump = self.model_dump()
        del model_dump['creation_timestamp_utc']
        del model_dump['repo_subid']
        del model_dump['name']
        save_path.write_text(toml.dumps(model_dump))

    @classmethod
    def load(cls, config: repoyard.config.Config, storage_location_name: str, repo_full_name: str) -> None:
        repo_id, name = repo_full_name.split('__', 1)
        timestamp_len =_get_repo_timestamp_length(config)
        creation_timestamp = repo_id[:timestamp_len]
        repo_subid = repo_id[timestamp_len+1:]
        
        repometa_path = config.local_store_path / storage_location_name / repo_full_name / const.REPO_METAFILE_REL_PATH
        if not repometa_path.exists():
            raise ValueError(f"Repo meta file {repometa_path} does not exist.")
        
        return RepoMeta(**{
            **toml.loads(repometa_path.read_text()),
            'creation_timestamp_utc': creation_timestamp,
            'repo_subid': repo_subid,
            'name': name,
            'storage_location': storage_location_name,
        })

    @classmethod
    def validate_group_name(cls, group_name: str) -> None:
        """
        Allowed characters: alphanumeric + `_`, `-`, `/`
        """
        import re

        pattern = r'^[A-Za-z0-9_\-/]+$'
        if not isinstance(group_name, str) or not re.match(pattern, group_name):
            raise ValueError(
                f"Invalid group name '{group_name}'. "
                "Allowed characters: alphanumeric, '_', '-', '/'."
            )
            
    @model_validator(mode='after')
    def validate_repo_meta(self):
        if len(self.groups) != len(set(self.groups)):
            raise ValueError("Groups must be unique.")

        for group_name in self.groups:
            self.validate_group_name(group_name)

        # Test that the creation timestamp is valid
        try:
            self.creation_timestamp_datetime
        except ValueError:
            raise ValueError("Creation timestamp is not valid.")
        
        return self


# %%
from tests.utils import create_repoyards
from repoyard.config import get_config

sl_name, _, _, config_path, data_path = create_repoyards()
config = get_config(config_path)

repo_meta = RepoMeta.create(config, "my_repo", sl_name, "creator_hostname", [])
repo_meta.save(config)
_repo_meta = RepoMeta.load(config, sl_name, repo_meta.full_name)

assert repo_meta.model_dump_json() == _repo_meta.model_dump_json()


# %% [markdown]
# # `RepoyardMeta`

# %%
#|export
class RepoyardMeta(const.StrictModel):
    repo_metas: list[RepoMeta]

    @property
    def by_storage_location(self) -> dict[str, dict[str, RepoMeta]]:
        if not hasattr(self, '__by_storage_location'):
            self.__by_storage_location = {
                sl_name: {
                    repo_meta.full_name: repo_meta
                    for repo_meta in self.repo_metas
                    if repo_meta.storage_location == sl_name
            }
            for sl_name in self.by_storage_location
        }
        return self.__by_storage_location

    @property
    def by_id(self) -> dict[str, RepoMeta]:
        if not hasattr(self, '__by_id'):
            self.__by_id = {
                repo_meta.repo_id: repo_meta
                for repo_meta in self.repo_metas
            }
        return self.__by_id

    @property
    def by_full_name(self) -> dict[str, RepoMeta]:
        if not hasattr(self, '__by_full_name'):
            self.__by_full_name = {
                repo_meta.full_name: repo_meta
                for repo_meta in self.repo_metas
            }
        return self.__by_full_name



# %%
#|export
def create_repoyard_meta(
    config: repoyard.config.Config
) -> RepoyardMeta:
    """Create a dict of all repo metas. To be saved in `config.repoyard_meta_path`."""
    repo_metas = []
    for storage_location_name in config.storage_locations:
        local_storage_location_path = config.local_store_path / storage_location_name
        for repo_path in local_storage_location_path.glob('*'):
            if repo_path.is_file(): continue
            repo_metas.append(RepoMeta.load(config, storage_location_name, repo_path.name))
    return RepoyardMeta(repo_metas=repo_metas)


# %%
#|export
def refresh_repoyard_meta(
    config: repoyard.config.Config,
) -> RepoyardMeta:
    repoyard_meta = create_repoyard_meta(config)
    config.repoyard_meta_path.write_text(repoyard_meta.model_dump_json())


# %%
#|export
def get_repoyard_meta(
    config: repoyard.config.Config,
    force_create: bool=False,
) -> RepoyardMeta:
    if not config.repoyard_meta_path.exists() or force_create:
        refresh_repoyard_meta(config)
    return RepoyardMeta.model_validate_json(config.repoyard_meta_path.read_text())


# %%
#|export
def get_repo_group_configs(
    config: repoyard.config.Config,
    repo_metas: list[RepoMeta],
) -> dict[str, RepoGroupConfig]:
    repo_group_configs = config.repo_groups.copy()
    for repo_meta in repo_metas:
        for group_name in repo_meta.groups:
            if group_name not in repo_group_configs:
                repo_group_configs[group_name] = RepoGroupConfig()
    return repo_group_configs, config.virtual_repo_groups


# %%
#|export
def create_user_repo_group_symlinks(
    config: repoyard.config.Config,
):
    from collections import defaultdict
    from repoyard.config import RepoGroupTitleMode, VirtualRepoGroupConfig
    repo_metas = [repo_meta for repo_meta in get_repoyard_meta(config).repo_metas if repo_meta.check_included(config)]
    repo_metas.sort(key=lambda x: x.creation_timestamp_datetime)
    groups, virtual_repo_groups = get_repo_group_configs(config, repo_metas)
    symlink_paths = []

    for vg in virtual_repo_groups:
        if vg in groups:
            print(f"Warning: Virtual repo group '{vg}' is also a regular repo group.")
    groups.update(virtual_repo_groups)

    def _get_symlink_title(repo_meta: RepoMeta, group_config: RepoGroupConfig) -> str:
        if group_config.repo_title_mode == RepoGroupTitleMode.FULL_NAME:
            title = repo_meta.full_name
        elif group_config.repo_title_mode == RepoGroupTitleMode.DATETIME_AND_NAME:
            title = f"{repo_meta.creation_timestamp_utc}__{repo_meta.name}"
        elif group_config.repo_title_mode == RepoGroupTitleMode.NAME:
            title = repo_meta.name
        else:
            raise Exception(f"Invalid repo title mode: {group_config.repo_title_mode}")
        return title

    # Generate all symlink paths to create
    _symlinks = []
    for group_name, group_config in groups.items():
        title_counter = defaultdict(int)
        for repo_meta in repo_metas:
            if not repo_meta.check_included(config): continue
            if isinstance(group_config, VirtualRepoGroupConfig):
                if not group_config.is_in_group(repo_meta.groups): continue
            else:
                if group_name not in repo_meta.groups: continue
            dest_path = repo_meta.get_local_part_path(config, RepoPart.DATA)
            title = _get_symlink_title(repo_meta, group_config)
            if title_counter[title] > 1:
                title = f"{title} (CONFLICT {title_counter[title]})" # TODO this will break if the title contains a `(CONFLICT ...`
            title_counter[title] += 1
            symlink_path = config.user_repo_groups_path / group_name / title   
            _symlinks.append((dest_path, symlink_path))

    # Remove all existing symlinks that are not in the _symlinks list
    for path in config.user_repo_groups_path.glob('**/*'):
        if path in _symlinks: continue
        if path.is_symlink():
            path.unlink()

    # Now check for any remaining debris
    _symlink_paths = [symlink_path for _, symlink_path in _symlinks]
    def _inspect_folder(path: Path) -> None:
        if path.is_symlink(): return
        for p in path.iterdir():
            if p.is_dir():
                _inspect_folder(p)
            else:
                if p not in _symlink_paths:
                    raise Exception(f"File '{p}' is in the user repo group path '{config.user_repo_groups_path}'.")
    for path in config.user_repo_groups_path.glob('*'):
        if path.is_dir():
            _inspect_folder(path)
        else:
            raise Exception(f"'{path}' is in the user repo group path '{config.user_repo_groups_path}' but is not a directory!")

    # Create the symlinks
    for dest_path, symlink_path in _symlinks:
        symlink_path.parent.mkdir(parents=True, exist_ok=True)
        if symlink_path.exists():
            if symlink_path.is_symlink():
                if symlink_path.resolve() != dest_path.resolve():
                    symlink_path.unlink()
                else:
                    continue # The symlink already points to the correct destination so leave it as it is
            else:
                raise Exception(f"'{symlink_path}' is in the user repo group path '{config.user_repo_groups_path}' but is not a symlink!")
        symlink_path.symlink_to(dest_path, target_is_directory=True)

    # Remove all empty group folders that are not existing groups
    def _remove_empty_non_group_folders(path: Path) -> None:
        if path.is_symlink(): return
        for p in path.iterdir():
            if p.is_dir():
                _remove_empty_non_group_folders(p)
        is_group_folder = path.relative_to(config.user_repo_groups_path).as_posix() in groups
        if not is_group_folder and len(list(path.iterdir())) == 0:
            path.rmdir()
    for path in config.user_repo_groups_path.glob('*'):
        _remove_empty_non_group_folders(path)


# %% [markdown]
# # `SyncRecord`

# %%
#|export
class SyncRecord(const.StrictModel):
    ulid: ULID = Field(default_factory=ULID)
    timestamp: datetime|None = None # Is set after validation. It's just to read it easier in printouts.
    sync_complete: bool
    syncer_hostname: str

    @classmethod
    def create(cls, sync_complete: bool, syncer_hostname: str|None=None) -> None:
        from repoyard._utils import get_hostname
        return SyncRecord(
            sync_complete=sync_complete,
            syncer_hostname=syncer_hostname or get_hostname(),
        )

    async def rclone_save(self, rclone_config_path: str, dest: str, dest_path: str) -> None:
        from repoyard._utils import rclone_copyto
        import tempfile
        temp_path = Path(tempfile.mkstemp(suffix='.json')[1])
        temp_path.write_text(self.model_dump_json())
        await rclone_copyto(
            rclone_config_path=rclone_config_path,
            source="",
            source_path=temp_path.as_posix(),
            dest=dest,
            dest_path=dest_path,
            dry_run=False,
        )

    @classmethod
    async def rclone_read(cls, rclone_config_path: str, source: str, sync_record_path: str) -> str:
        from repoyard._utils import rclone_cat
        sync_record_exists, sync_record = await rclone_cat(
            rclone_config_path=rclone_config_path,
            source=source,
            source_path=sync_record_path,
        )
        
        if sync_record_exists:
            return SyncRecord.model_validate_json(sync_record)
        else:
            return None

    @model_validator(mode='after')
    def validate_timestamp(self):
        if self.timestamp is None:
            self.timestamp = self.ulid.datetime
        if self.timestamp != self.ulid.datetime:
            raise ValueError("`timestamp` should be set to the ULID's datetime.")
        return self


# %%
#|export
from typing import NamedTuple

class SyncCondition(Enum):
    SYNCED = "synced"
    SYNC_INCOMPLETE = "sync_incomplete"
    CONFLICT = "conflict"
    NEEDS_PUSH = "needs_push"
    NEEDS_PULL = "needs_pull"

class SyncStatus(NamedTuple):
    sync_condition: SyncCondition
    local_path_exists: bool
    remote_path_exists: bool
    local_sync_record: SyncRecord
    remote_sync_record: SyncRecord
    is_dir: bool


# %%
#|export
async def get_sync_status(
    rclone_config_path: str,
    local_path: str,
    local_sync_record_path: str,
    remote: str,
    remote_path: str,
    remote_sync_record_path: str,
) -> SyncStatus:
    from repoyard._utils import check_last_time_modified
    from repoyard._utils import rclone_path_exists

    local_path_exists, local_path_is_dir = await rclone_path_exists(
        rclone_config_path=rclone_config_path,
        source="",
        source_path=local_path,
    )
    if local_path_is_dir and local_path_exists:
        local_path_is_empty = len(list(local_path.iterdir())) == 0 # a bit hacky

    remote_path_exists, remote_path_is_dir = await rclone_path_exists(
        rclone_config_path=rclone_config_path,
        source=remote,
        source_path=remote_path,
    )

    if (local_path_exists and remote_path_exists) and (local_path_is_dir != remote_path_is_dir):
        _local = "directory" if local_path_is_dir else "file"
        _remote = "directory" if remote_path_is_dir else "file"
        raise Exception(f"Local and remote paths are not both files or both directories. Local is {_local} and remote is {_remote}. Local path: '{local_path}', remote path: '{remote_path}'.")
    
    is_dir = local_path_is_dir or remote_path_is_dir

    local_sync_record = await SyncRecord.rclone_read(
        rclone_config_path=rclone_config_path,
        source="",
        sync_record_path=local_sync_record_path,
    )

    remote_sync_record = await SyncRecord.rclone_read(
        rclone_config_path=rclone_config_path,
        source=remote,
        sync_record_path=remote_sync_record_path,
    )

    local_sync_incomplete = local_sync_record is not None and not local_sync_record.sync_complete
    remote_sync_incomplete = remote_sync_record is not None and not remote_sync_record.sync_complete

    sync_records_match = (local_sync_record is not None and remote_sync_record is not None) and \
        (local_sync_record.ulid == remote_sync_record.ulid)

    if remote_path_exists and remote_sync_record is None:
        raise Exception(f"Something wrong here. Remote path exists, but remote sync record does not exist. Local path: '{local_path}', remote path: '{remote_path}.")

    local_last_modified = check_last_time_modified(local_path)

    if local_last_modified is None and local_path_exists:
        if (not local_path_is_dir) or (local_path_is_dir and not local_path_is_empty):
            # Logic here: If the local path is a file, it should be able to be checked for last modification.
            # If the local path is a non-empty directory, it should also be able to be checked for last modification.
            raise Exception(f"Something wrong here. Local path exists and is not empty, but cannot be checked for last modification. Local path: '{local_path}', remote path: '{remote_path}.")

    if local_sync_incomplete or remote_sync_incomplete:
        sync_condition = SyncCondition.SYNC_INCOMPLETE
    else:
        if sync_records_match:
            if local_last_modified is not None and local_last_modified > local_sync_record.timestamp:
                sync_condition = SyncCondition.NEEDS_PUSH
            else:
                sync_condition = SyncCondition.SYNCED
        else:
            if local_path_exists:
                if remote_path_exists:
                    if local_sync_record is None:
                        raise Exception(f"Something wrong here. Local sync record does not exist, but the local and remote path exists. Local path: '{local_path}', remote path: '{remote_path}.")
                    remote_sync_more_recent = remote_sync_record.ulid.datetime > local_sync_record.ulid.datetime
                    if remote_sync_more_recent:
                        if local_last_modified is not None and local_last_modified > local_sync_record.timestamp:
                            sync_condition = SyncCondition.CONFLICT
                        else:
                            sync_condition = SyncCondition.NEEDS_PULL
                    else:
                        sync_condition = SyncCondition.CONFLICT
                else:
                    sync_condition = SyncCondition.NEEDS_PUSH
            else:
                if remote_path_exists:
                    sync_condition = SyncCondition.NEEDS_PULL
                else:
                    sync_condition = SyncCondition.SYNCED # Synced by default, since neither local nor remote path exists. This will often be the case for `conf`, for example.




    if local_sync_incomplete or remote_sync_incomplete:
        sync_condition = SyncCondition.SYNC_INCOMPLETE
    else:
        if sync_records_match:
            if local_last_modified is not None and local_last_modified > local_sync_record.timestamp:
                sync_condition = SyncCondition.NEEDS_PUSH
            else:
                sync_condition = SyncCondition.SYNCED
        else:
            if local_path_exists:
                if not remote_path_exists:
                    sync_condition = SyncCondition.NEEDS_PUSH
                else:
                    if local_sync_record is None:
                        raise Exception("Something wrong here. Local sync record does not exist, but the remote path exists.")
                    elif local_last_modified is not None and local_last_modified > local_sync_record.timestamp:
                        sync_condition = SyncCondition.CONFLICT
                    else:
                        sync_condition = SyncCondition.NEEDS_PULL
            else:
                if remote_path_exists:
                    sync_condition = SyncCondition.NEEDS_PULL
                else:
                    sync_condition = SyncCondition.SYNCED # Synced by default, since neither local nor remote path exists

    return SyncStatus(
        sync_condition=sync_condition,
        local_path_exists=local_path_exists,
        remote_path_exists=remote_path_exists,
        local_sync_record=local_sync_record,
        remote_sync_record=remote_sync_record,
        is_dir=is_dir,
    )

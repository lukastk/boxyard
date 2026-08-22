# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # config

# %%
#|default_exp config

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
from pydantic import model_validator
from pathlib import Path
import tomllib
import os
from enum import Enum

from boxyard import const

# %% [markdown]
# # `config.json`

# %%
#|export
class StorageType(Enum):
    RCLONE = "rclone"
    LOCAL = "local"


class StorageConfig(const.StrictModel):
    storage_type: StorageType
    store_path: Path

    @model_validator(mode="after")
    def validate_config(self):
        # Expand paths
        self.store_path = self.store_path.expanduser()
        return self


class BoxGroupTitleMode(Enum):
    INDEX_NAME = "index_name"
    DATETIME_AND_NAME = "datetime_and_name"
    NAME = "name"


class BoxGroupConfig(const.StrictModel):
    symlink_name: str | None = None
    box_title_mode: BoxGroupTitleMode = BoxGroupTitleMode.INDEX_NAME
    unique_box_names: bool = False


class VirtualBoxGroupConfig(const.StrictModel):
    symlink_name: str | None = None
    box_title_mode: BoxGroupTitleMode = BoxGroupTitleMode.INDEX_NAME
    filter_expr: str

    def is_in_group(self, groups: list[str]) -> bool:
        if not hasattr(self, "_filter_func"):
            from boxyard._utils.logical_expressions import get_group_filter_func

            self._filter_func = get_group_filter_func(self.filter_expr)
        return self._filter_func(groups)

    @model_validator(mode="after")
    def validate_filter_expr(self):
        """
        Reject a malformed `filter_expr` when the config is loaded, rather than
        when the group is first evaluated.

        The evaluation below is load-bearing, not a smoke test:
        `get_group_filter_func` only TOKENIZES eagerly and re-parses the token
        stream on every call, so a structural error such as `"(a AND b"`
        compiles fine and only raises when the predicate is invoked. That meant
        a typo in config.toml surfaced far from its cause -- during symlink
        building -- instead of at load.

        The parser never consults the group set for control flow (there is no
        short-circuiting; the right-hand side is always evaluated before
        combining), so any input exercises the whole parse. `[]` is as good as
        anything.
        """
        try:
            self.is_in_group([])
        except Exception as e:
            raise ValueError(f"Invalid `filter_expr` {self.filter_expr!r}: {e}") from e
        return self


class BoxTimestampFormat(Enum):
    DATE_AND_TIME = "date_and_time"
    DATE_ONLY = "date_only"


class Config(const.StrictModel):
    config_path: Path  # Path to the config file. Will not be saved to the config file.

    default_storage_location: str
    boxyard_data_path: Path
    box_timestamp_format: BoxTimestampFormat
    user_boxes_path: Path
    user_box_groups_path: Path
    storage_locations: dict[str, StorageConfig]
    box_groups: dict[str, BoxGroupConfig]
    virtual_box_groups: dict[str, VirtualBoxGroupConfig]
    default_box_groups: list[str]
    box_subid_character_set: str
    box_subid_length: int
    max_concurrent_rclone_ops: int

    # Optional explicit path to the rclone binary. If unset, boxyard resolves rclone
    # via the BOXYARD_RCLONE env var, PATH, then known install dirs (see _utils.rclone).
    rclone_path: Path | None = None

    # Parent-child settings
    single_parent: bool = False  # If True, each box can have at most one parent

    # New box creation settings
    sync_before_new_box: bool = False  # If True, sync boxmetas before creating new box to check for ID collisions on remote

    @property
    def local_store_path(self) -> Path:
        return self.boxyard_data_path / "local_store"

    @property
    def local_sync_backups_path(self) -> Path:
        return self.boxyard_data_path / "sync_backups"

    @property
    def boxyard_meta_path(self) -> Path:
        return self.boxyard_data_path / "boxyard_meta.json"

    @property
    def rclone_config_path(self) -> Path:
        return Path(self.config_path).parent / "boxyard_rclone.conf"

    @property
    def default_rclone_exclude_path(self) -> Path:
        return self.config_path.parent / "default.rclone_exclude"

    @property
    def remote_indexes_path(self) -> Path:
        """Path to cached remote index lookups (box_id -> remote index_name)."""
        return self.boxyard_data_path / "remote_indexes"

    @model_validator(mode="after")
    def validate_config(self):
        # Expand all paths
        self.config_path = Path(self.config_path).expanduser()
        self.boxyard_data_path = Path(self.boxyard_data_path).expanduser()
        self.user_boxes_path = Path(self.user_boxes_path).expanduser()
        self.user_box_groups_path = Path(self.user_box_groups_path).expanduser()
        if self.rclone_path is not None:
            self.rclone_path = Path(self.rclone_path).expanduser()

        import re

        for name in self.storage_locations.keys():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError(
                    f"StorageConfig name {name} is invalid. StorageConfig names can only contain alphanumeric characters, underscore(_), or dash(-)."
                )

        if len(self.storage_locations) == 0:
            raise ValueError("No storage locations defined.")

        # Check that the default storage location exists
        if not any(
            name == self.default_storage_location for name in self.storage_locations
        ):
            raise ValueError(
                f"default_storage_location '{self.default_storage_location}' not found in storage_locations"
            )

        from boxyard._models import BoxMeta

        for group_name in list(self.box_groups.keys()) + list(
            self.virtual_box_groups.keys()
        ):
            BoxMeta.validate_group_name(group_name)

        return self

# %%
#|export
def get_config(path: Path | None = None) -> Config:
    if path is None:
        path = const.DEFAULT_CONFIG_PATH
    path = Path(path).expanduser()
    with open(path, "rb") as _f:
        config_dict = {"config_path": path, **tomllib.load(_f)}

    # Additively merge default_box_groups from env var (TOML list string, e.g. '["ctx/mac", "ctx/linux"]')
    env_groups = os.environ.get(const.ENV_VAR_DEFAULT_BOX_GROUPS)
    if env_groups:
        extra = tomllib.loads(f"v = {env_groups}")["v"]
        existing = config_dict.get("default_box_groups", [])
        config_dict["default_box_groups"] = list(dict.fromkeys(existing + extra))

    return Config(**config_dict)

# %%
#|export
def _get_default_config_dict(config_path=None, data_path=None) -> Config:
    if config_path is None:
        config_path = const.DEFAULT_CONFIG_PATH
    if data_path is None:
        data_path = const.DEFAULT_DATA_PATH
    config_path = Path(config_path)
    data_path = Path(data_path)

    config_dict = dict(
        config_path=config_path.as_posix(),
        default_storage_location="fake",
        boxyard_data_path=data_path.as_posix(),
        box_timestamp_format=BoxTimestampFormat.DATE_ONLY.value,
        user_boxes_path=const.DEFAULT_USER_BOXES_PATH.as_posix(),
        user_box_groups_path=const.DEFAULT_USER_BOX_GROUPS_PATH.as_posix(),
        storage_locations={
            "fake": dict(
                storage_type=StorageType.LOCAL.value,
                store_path=(data_path / const.DEFAULT_FAKE_STORE_REL_PATH).as_posix(),
            )
        },
        box_groups={},
        virtual_box_groups={},
        default_box_groups=[],
        box_subid_character_set=const.DEFAULT_BOX_SUBID_CHARACTER_SET,
        box_subid_length=const.DEFAULT_BOX_SUBID_LENGTH,
        max_concurrent_rclone_ops=const.DEFAULT_MAX_CONCURRENT_RCLONE_OPS,
        single_parent=False,
        sync_before_new_box=False,
    )
    return config_dict

# %% [markdown]
# # `rclone.conf`

# %%
#|exporti
_default_rclone_config = """
"""

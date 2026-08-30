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
from typing import Any, get_args, get_origin
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
        self.store_path = self.store_path.expanduser()
        return self


class CheckoutRootConfig(const.StrictModel):
    """One machine-local root where box DATA may be checked out.

    ``mount_target`` and ``filesystem_uuid`` form an optional guard. When set,
    Boxyard requires that exact path to be a mount point backed by that UUID
    before it reads or mutates this root. They are a pair: specifying only one
    is a configuration error.
    """

    path: Path
    mount_target: Path | None = None
    filesystem_uuid: str | None = None

    @model_validator(mode="after")
    def validate_config(self):
        self.path = self.path.expanduser().absolute()
        if self.mount_target is not None:
            self.mount_target = self.mount_target.expanduser().absolute()
        if (self.mount_target is None) != (self.filesystem_uuid is None):
            raise ValueError(
                "checkout-root mount_target and filesystem_uuid must be configured together"
            )
        if self.mount_target is not None:
            try:
                self.path.relative_to(self.mount_target)
            except ValueError as e:
                raise ValueError(
                    f"checkout-root path '{self.path}' must be the mount target "
                    f"'{self.mount_target}' or a directory beneath it"
                ) from e
        if self.filesystem_uuid is not None and not self.filesystem_uuid.strip():
            raise ValueError("checkout-root filesystem_uuid must not be empty")
        return self


class BoxGroupTitleMode(Enum):
    INDEX_NAME = "index_name"
    DATETIME_AND_NAME = "datetime_and_name"
    NAME = "name"


class BoxGroupConfig(const.StrictModel):
    symlink_name: str | None = None
    box_title_mode: BoxGroupTitleMode = BoxGroupTitleMode.INDEX_NAME
    unique_box_names: bool = False


INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_interval(text: str, where: str) -> int:
    """
    Parse an interval like `6h`, `90m`, `7d` into whole SECONDS.

    `where` names the config location, so a bad value says which key is wrong
    rather than only that something is.

    Deliberately strict: no bare numbers (is `6` six seconds or six hours?), no
    compound forms (`1h30m`), no float. A cadence that silently means something
    other than what was written is worse than a refusal, and every real cadence
    in this design is expressible as one unit.
    """
    raw = text.strip()
    if not raw:
        raise ValueError(f"{where}: interval is empty")
    unit = raw[-1].lower()
    if unit not in INTERVAL_UNITS:
        raise ValueError(
            f"{where}: interval {text!r} must end in one of "
            f"{'/'.join(sorted(INTERVAL_UNITS))} (e.g. '6h', '90m', '7d')"
        )
    number = raw[:-1]
    if not number.isdigit():
        raise ValueError(
            f"{where}: interval {text!r} must be a whole number followed by a "
            f"unit (e.g. '6h'); got {number!r} before {unit!r}"
        )
    seconds = int(number) * INTERVAL_UNITS[unit]
    if seconds <= 0:
        raise ValueError(f"{where}: interval {text!r} must be greater than zero")
    return seconds


class SyncPolicyConfig(const.StrictModel):
    """
    One named sync policy: how often a box's parts are checked, and whether its
    DATA is stored packed.

    Every field is OPTIONAL, and `None` means "not stated at this level" rather
    than "off". That is what makes resolution work per DIMENSION -- a box can
    take its DATA cadence from `conf/sync.toml` and its META cadence from the
    group policy. `None` here is a legitimate expected state, not a masked bug.

    There is deliberately NO `compress` here. Compression is a property of the
    storage BACKEND, not a scheduling policy: a restic-backed box is compressed
    and deduplicated because that is what the backend does, and no per-box knob
    would change it. The field existed briefly, implemented nothing, and was
    removed once the measurements settled the direction (jackfruit-hq:
    687,876 remote objects -> 56, 7.52 GiB -> 0.72 GiB). If a choice of storage
    format is ever wanted it belongs here as `storage_format`, which is a
    different question with a different answer.

    `groups` lists the box groups this policy applies to. A policy with no
    groups is reachable only by name from a box's own `conf/sync.toml`.
    """

    data_interval: str | None = None
    meta_interval: str | None = None
    groups: list[str] = []

    def interval_seconds(self, part: str, policy_name: str) -> int | None:
        text = self.data_interval if part == "data" else self.meta_interval
        if text is None:
            return None
        return parse_interval(
            text, f"sync_policies.{policy_name}.{part}_interval"
        )


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
    # Permanent legacy/default-root schema: user_boxes_path is the checkout root
    # named "default". Additional named roots live in checkout_roots. Keeping
    # this field is intentional, not a transition shim: old configs are complete
    # multi-root configs with exactly one root and need no rewrite.
    user_boxes_path: Path
    checkout_roots: dict[str, CheckoutRootConfig] = {}
    user_box_groups_path: Path
    storage_locations: dict[str, StorageConfig]
    box_groups: dict[str, BoxGroupConfig]
    virtual_box_groups: dict[str, VirtualBoxGroupConfig]
    sync_policies: dict[str, SyncPolicyConfig] = {}
    default_box_groups: list[str]
    box_subid_character_set: str
    box_subid_length: int
    max_concurrent_rclone_ops: int

    # Optional explicit path to the rclone binary. If unset, boxyard resolves rclone
    # via the BOXYARD_RCLONE env var, PATH, then known install dirs (see _utils.rclone).
    rclone_path: Path | None = None

    # This machine's stable name within the yard -- the value that will be
    # written as a box's `write_owner`. Configured, never derived: hostnames
    # are not an identity (one machine in this fleet has reported both
    # `lukas-pocket4` and `pocket4`, and macOS reports editable pretty names
    # like `Lukas’s MacBook Pro`), so guessing one would hand a box to a
    # machine that can no longer prove it is the same machine.
    #
    # Optional, because making it required would break every machine's config
    # on upgrade until myrig renders it. A machine without a name simply can
    # never be an owner, which is the safe direction; `doctor` reports it as
    # `machine-name-unset`. Overridable by BOXYARD_MACHINE_NAME, which is what
    # tests and one-offs use -- note that the supervisor that runs the syncs
    # does not source an interactive shell's environment, so the env var
    # cannot be the delivery mechanism for the real value.
    machine_name: str | None = None

    # Parent-child settings
    single_parent: bool = False  # If True, each box can have at most one parent

    # New box creation settings
    sync_before_new_box: bool = False  # If True, sync boxmetas before creating new box to check for ID collisions on remote

    # Conflict resolution
    #
    # When True, a boxmeta that BOTH sides have edited is merged against the
    # copy they last agreed on (`meta.base.toml`) instead of refusing. `groups`
    # and `parents` merge as sets; a scalar both sides changed differently is
    # still a refusal for a human to settle.
    #
    # OFF by default, and deliberately so. Resolving the merge means
    # force-pushing the result over the remote boxmeta -- safe, because the
    # merge CONTAINS what the remote had, but still a write that today's code
    # would refuse to make. That is a decision to take per fleet rather than
    # one that arrives with an upgrade.
    merge_diverged_boxmetas: bool = False

    # Forward-compat passthrough: keys found in config.toml that this version
    # of boxyard does not know. `get_config` collects them here instead of
    # letting `extra="forbid"` reject the file.
    #
    # Keyed by DOTTED PATH, so it covers the whole file and not just its top
    # level: a key inside `[storage_locations.X]`, `[box_groups.X]` or
    # `[virtual_box_groups.X]` arrives here as
    # `storage_locations.X.some_key`. Those entries are StrictModels too, so
    # tolerating only the top level would have left the same trap one level
    # down -- and a nested addition is not hypothetical: `symlink_name` was
    # added to both group models in `8d9e074`. `get_config` derives the tables
    # to walk from the annotations, so a config model added later is covered
    # without anyone remembering to update a list.
    #
    # `config.toml` is the same trap `boxmeta.toml` was, for the same reason
    # and with a wider blast radius: it is a StrictModel too, so a key added
    # for a newer boxyard makes EVERY command fail on any machine that does not
    # know it -- and on this fleet the file is one myrig-rendered artefact
    # shared by every machine, so one addition breaks them all at once.
    #
    # Read the limit of this carefully: it does NOT rescue a machine already
    # running a version without it. Tolerance has to be deployed BEFORE the key
    # it tolerates, so the rollout order still stands -- boxyard everywhere
    # first, then the config change. Its value is forward-looking: from v0.5.0
    # on, a config addition costs an older machine a doctor finding instead of
    # a machine that cannot run boxyard at all.
    #
    # Unlike `BoxMeta.unknown_keys` this is not written back, because boxyard
    # never rewrites config.toml -- `init` creates it, and nothing else touches
    # it. The container exists so the keys can be reported rather than vanish
    # silently: `extra="forbid"` is what catches a TYPO'd key today, and
    # tolerating unknown keys without reporting them would trade a loud typo
    # for a silent one. `doctor`'s `unknown-config-keys` is that report, and it
    # names the dotted path so the finding says WHERE the key is.
    unknown_keys: dict[str, Any] = {}

    @property
    def configured_checkout_roots(self) -> dict[str, CheckoutRootConfig]:
        """All roots, including the permanent implicit ``default`` root."""
        return {
            "default": CheckoutRootConfig(path=self.user_boxes_path),
            **self.checkout_roots,
        }

    @property
    def default_checkout_root_name(self) -> str:
        return "default"

    @property
    def placements_path(self) -> Path:
        return self.boxyard_data_path / "placements"

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

        # A machine_name that could never be written as a `write_owner` is a
        # configuration error, not something to discover later at claim time
        # on one machine only. Fail here, where the message names the file.
        if self.machine_name is not None and not re.fullmatch(
            const.MACHINE_NAME_REGEX, self.machine_name
        ):
            raise ValueError(
                f"Invalid machine_name {self.machine_name!r} in '{self.config_path}'. "
                f"It must match {const.MACHINE_NAME_REGEX} (alphanumeric, '_', '-'; "
                "1-64 characters). Use the machine's canonical short name, e.g. "
                "'macbook' or 'mymain'."
            )

        # The passthrough must stay disjoint from the fields this version owns,
        # or a report of "keys boxyard does not know" would name one it does.
        _shadowed = set(self.unknown_keys) & set(type(self).model_fields)
        if _shadowed:
            raise ValueError(
                f"unknown_keys must not contain keys boxyard knows: {sorted(_shadowed)}"
            )

        for name in self.storage_locations.keys():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError(
                    f"StorageConfig name {name} is invalid. StorageConfig names can only contain alphanumeric characters, underscore(_), or dash(-)."
                )

        if len(self.storage_locations) == 0:
            raise ValueError("No storage locations defined.")

        if self.default_storage_location not in self.storage_locations:
            raise ValueError(
                f"default_storage_location '{self.default_storage_location}' not found in storage_locations"
            )

        if "default" in self.checkout_roots:
            raise ValueError(
                "checkout_roots may not define the reserved name 'default'; "
                "user_boxes_path is permanently the checkout root named 'default'"
            )
        for name in self.checkout_roots:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError(
                    f"Checkout root name {name!r} is invalid. Names may contain only "
                    "alphanumeric characters, underscore, or dash."
                )

        from boxyard._models import BoxMeta

        for group_name in list(self.box_groups.keys()) + list(
            self.virtual_box_groups.keys()
        ):
            BoxMeta.validate_group_name(group_name)

        return self

# %% [markdown]
# ## Reading the config file tolerantly
#
# The tolerance is scoped to *reading a file*, exactly as `BoxMeta.load`'s is,
# and deliberately not implemented as a validator on the models themselves: a
# `Config(...)` or `StorageConfig(...)` built in code should still reject a
# misspelled keyword loudly, since no `doctor` check can see those.
#
# Unknown keys are collected into ONE flat mapping on `Config`, keyed by their
# dotted path (`storage_locations.hetzner-box.some_key`). Nothing is written
# back — boxyard never rewrites `config.toml` — so the keys only ever need to
# be *reported*, and a path reports where the key actually is. That also means
# the nested models need no `unknown_keys` field of their own.

# %%
#|exporti
def _split_known_keys(
    parsed: dict, model: type, path_prefix: str
) -> tuple[dict, dict]:
    """
    Split `parsed` into (keys `model` declares, keys it does not).

    Unknown keys come back keyed by `path_prefix + key`, so the caller can
    merge every level's leftovers into one flat mapping without losing track
    of where each key came from.
    """
    known, unknown = {}, {}
    for key, value in parsed.items():
        if key in model.model_fields:
            known[key] = value
        else:
            unknown[f"{path_prefix}{key}"] = value
    return known, unknown


def _nested_model_tables() -> dict[str, type]:
    """
    The `Config` fields shaped `dict[str, <some StrictModel>]` — i.e. the TOML
    tables whose entries need the same tolerance as the top level.

    Derived from the annotations rather than hardcoded, so a config model added
    later is covered without anyone having to remember this function exists.
    Forgetting it would reintroduce exactly the gap this closes, in a place no
    test would obviously cover.
    """
    tables = {}
    for name, field in Config.model_fields.items():
        if get_origin(field.annotation) is not dict:
            continue
        args = get_args(field.annotation)
        if (
            len(args) == 2
            and isinstance(args[1], type)
            and issubclass(args[1], const.StrictModel)
        ):
            tables[name] = args[1]
    return tables

# %%
#|export
def get_config(path: Path | None = None) -> Config:
    if path is None:
        path = const.DEFAULT_CONFIG_PATH
    path = Path(path).expanduser()
    with open(path, "rb") as _f:
        parsed = tomllib.load(_f)

    # Split the file into keys this version knows and keys it does not, so a
    # config written for a newer boxyard does not make every command on this
    # machine fail. See `Config.unknown_keys` for why this does not retroactively
    # help a machine running an older version.
    #
    # `unknown_keys` is the container itself, never a key of the file; a config
    # that carries it is corrupt rather than newer, so say so.
    if "unknown_keys" in parsed:
        raise ValueError(
            f"Config file '{path}' contains the reserved key 'unknown_keys', which "
            "boxyard uses internally to carry keys written by a newer version. "
            "Remove it from the file."
        )

    known, unknown_keys = _split_known_keys(parsed, Config, "")

    # The nested tables need the same tolerance, and for the same reason: the
    # entries of `[storage_locations.X]`, `[box_groups.X]` and
    # `[virtual_box_groups.X]` are StrictModels too, so a key added to one of
    # them by a newer boxyard would break every older machine exactly as a
    # top-level key would. That is not hypothetical -- `symlink_name` was added
    # to both group models in `8d9e074`.
    for table_name, entry_model in _nested_model_tables().items():
        entries = known.get(table_name)
        if not isinstance(entries, dict):
            continue  # absent, or the wrong shape -- let the model raise on it
        cleaned = {}
        for entry_name, entry in entries.items():
            if not isinstance(entry, dict):
                # An ENTRY that is not a table is deliberately NOT tolerated,
                # and this is the boundary of the forward compatibility above.
                # These tables map a name to a group or a storage location, so
                # a scalar in that position is not a newer boxyard adding an
                # option: that would be a new field inside an entry (handled
                # just below) or a new top-level key (handled already).
                #
                # It is reached by writing a key directly under the CONTAINER
                # rather than under one of its entries, which takes one of two
                # forms -- measured, not assumed:
                #
                #   virtual_box_groups.future = "x"     # dotted key, and only
                #                                       # BEFORE any [table]
                #                                       # header; a dotted key
                #                                       # is relative to the
                #                                       # table it sits under
                #   [virtual_box_groups]                # a bare container
                #   future = "x"                        # header, then a scalar
                #
                # Note what does NOT reach it: appending a line to the end of a
                # real config.toml. TOML lands that inside whatever table came
                # last, and in a populated config that is a SUB-table
                # (`[virtual_box_groups.archived-uncategorized]`), so the line
                # becomes an unknown key inside an entry and is TOLERATED. Only
                # a config whose last table is a bare container behaves the
                # other way.
                #
                # Either way the value is a line the author believed they were
                # adding somewhere else, so it raises: pydantic names the exact
                # path and says a table was expected, which beats silently
                # discarding the edit.
                cleaned[entry_name] = entry
                continue
            entry_known, entry_unknown = _split_known_keys(
                entry, entry_model, f"{table_name}.{entry_name}."
            )
            cleaned[entry_name] = entry_known
            unknown_keys.update(entry_unknown)
        known[table_name] = cleaned

    config_dict = {"config_path": path, **known, "unknown_keys": unknown_keys}

    # Additively merge default_box_groups from env var (TOML list string, e.g. '["ctx/mac", "ctx/linux"]')
    env_groups = os.environ.get(const.ENV_VAR_DEFAULT_BOX_GROUPS)
    if env_groups:
        extra = tomllib.loads(f"v = {env_groups}")["v"]
        existing = config_dict.get("default_box_groups", [])
        config_dict["default_box_groups"] = list(dict.fromkeys(existing + extra))

    # BOXYARD_MACHINE_NAME overrides the config key, following the
    # BOXYARD_CONFIG_PATH / BOXYARD_RCLONE precedent (and the DEFAULT_BOX_GROUPS
    # handling just above, whose empty-means-unset rule this matches: an empty
    # value leaves the config key in force rather than blanking it).
    env_machine_name = os.environ.get(const.ENV_VAR_BOXYARD_MACHINE_NAME)
    if env_machine_name:
        config_dict["machine_name"] = env_machine_name

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
        checkout_roots={},
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
        merge_diverged_boxmetas=False,
    )
    return config_dict

# %% [markdown]
# # `rclone.conf`

# %%
#|exporti
_default_rclone_config = """
"""

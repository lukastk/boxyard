# ---
# jupyter:
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _sync_missing_boxmetas

# %%
#|default_exp cmds._sync_missing_boxmetas
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path

from boxyard.config import get_config, StorageType
from boxyard._utils.sync_helper import (
    SyncFailed,
    SyncUnsafe,
    InvalidRemotePath,
    SyncStatus,
    SyncSetting,
    SyncDirection,
)
from boxyard._utils import (
    check_interrupted,
    enable_soft_interruption,
    SoftInterruption,
)
from boxyard import const

# %%
#|set_func_signature
async def sync_missing_boxmetas(
    config_path: Path,
    max_concurrent_rclone_ops: int | None = None,
    box_index_names: list[str] | None = None,
    storage_locations: list[str] | None = None,
    sync_setting: SyncSetting = SyncSetting.CAREFUL,
    sync_direction: SyncDirection | None = None,
    verbose: bool = False,
    soft_interruption_enabled: bool = True,
) -> tuple[
    list[str],
    list[
        tuple[
            bool, SyncFailed | SyncUnsafe | InvalidRemotePath | None, SyncStatus, bool
        ]
    ],
]:
    """
    """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.integration.conftest import create_boxyards

remote_name, remote_rclone_path, config, config_path, data_path = create_boxyards()

# %%
# Args (1/2)
config_path = config_path
max_concurrent_rclone_ops = None
box_index_names = None
storage_locations = None
sync_direction = None
verbose = True
soft_interruption_enabled = True

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

if box_index_names is not None and storage_locations is not None:
    raise ValueError("Cannot provide both `box_index_names` and `storage_locations`.")

if max_concurrent_rclone_ops is None:
    max_concurrent_rclone_ops = config.max_concurrent_rclone_ops

if soft_interruption_enabled:
    enable_soft_interruption()

# %%
# Set up synced boxes
from boxyard.cmds import new_box, sync_box
import asyncio


async def _task(i):
    box_index_name = new_box(
        config_path=config_path, box_name=f"test_box{i}", storage_location="my_remote"
    )
    await sync_box(config_path=config_path, box_index_name=box_index_name)


await asyncio.gather(*[_task(i) for i in range(3)])

# %% [markdown]
# Sync remote boxmetas that have not been synced locally already (i.e. 'undiscovered' boxmetas)

# %%
#|export
if check_interrupted():
    raise SoftInterruption()

from boxyard._utils import rclone_lsjson, rclone_sync, async_throttler
from boxyard._models import BoxMeta, SyncRecord, BoxPart

for sl_name, sl_config in config.storage_locations.items():
    if sl_config.storage_type == StorageType.LOCAL:
        continue

    if storage_locations is not None and sl_name not in storage_locations:
        continue

    # Get remote boxmetas
    _ls_remote = await rclone_lsjson(
        config.rclone_config_path,
        source=sl_name,
        source_path=sl_config.store_path / const.REMOTE_BOXES_REL_PATH,
        files_only=True,
        recursive=True,
        # `- **` IS LOAD-BEARING. rclone has no implicit exclude: a filter list
        # of only `+` rules includes everything that does not match, so without
        # it this returns EVERY file at depth 2 and the `+` reads as a comment.
        # Harmless while `boxmeta.toml` is the only file a box directory holds
        # at that depth, and not harmless the moment anything else lands there.
        #
        # And the pattern must be anchored ONE LEVEL DOWN, not at the root:
        # measured, `+ /boxmeta.toml` with `- **` matches NOTHING, because
        # boxmetas live at `<box>/boxmeta.toml`. That combination would make
        # every box look permanently missing on a loop that runs every 15
        # minutes on every machine. Both listings below use the same filter on
        # purpose -- the code diffs the two sets, so they must agree.
        filter=[f"+ /*/{const.BOX_METAFILE_REL_PATH}", "- **"],
        max_depth=2,
    )
    _ls_remote = {f["Path"] for f in _ls_remote} if _ls_remote else set()

    _ls_local = await rclone_lsjson(
        config.rclone_config_path,
        source="",
        source_path=config.local_store_path / sl_name,
        files_only=True,
        recursive=True,
        filter=[f"+ /*/{const.BOX_METAFILE_REL_PATH}", "- **"],
        max_depth=2,
    )
    _ls_local = {f["Path"] for f in _ls_local} if _ls_local else set()

    # Reconcile on BOX ID, not on index name.
    #
    # A box's index name is `{box_id}__{name}`, and a rename changes only the
    # name half -- the id is preserved. Diffing the raw paths therefore treats a
    # renamed box as a brand-new one, and since this pass only ever ADDS, every
    # other machine ended up with TWO registrations for the same box id: its
    # stale pre-rename name, which nothing ever removed, plus the new one
    # fetched here. That is what `doctor`'s `duplicate-box-id` was reporting on
    # macbook, macstudio and ideapad (three boxes each, identical on all three,
    # absent on the machine that did the renames).
    def _index_names_by_box_id(paths: set[str]) -> dict[str, set[str]]:
        by_id: dict[str, set[str]] = {}
        for _p in paths:
            _index_name = Path(_p).parts[0]
            by_id.setdefault(BoxMeta.extract_box_id(_index_name), set()).add(_index_name)
        return by_id

    _remote_by_id = _index_names_by_box_id(_ls_remote)
    _local_by_id = _index_names_by_box_id(_ls_local)

    # A box that was RENAMED elsewhere: the same id, under a different name.
    # The remote is authoritative for names, so adopt it -- exactly what
    # `boxyard sync-name --direction to_local` does.
    #
    # Ambiguity is skipped rather than guessed at: more than one directory for
    # one id on either side means something is already wrong, and picking one
    # arbitrarily could rename a box onto a name that is already taken.
    _renames: list[tuple[str, str]] = []
    for _box_id, _remote_names in _remote_by_id.items():
        _local_names = _local_by_id.get(_box_id)
        if not _local_names or len(_remote_names) != 1 or len(_local_names) != 1:
            continue
        _remote_index_name = next(iter(_remote_names))
        _local_index_name = next(iter(_local_names))
        if _remote_index_name != _local_index_name:
            _renames.append((_local_index_name, _remote_index_name))

    if box_index_names is not None:
        _renames = [
            _r for _r in _renames
            if _r[0] in box_index_names or _r[1] in box_index_names
        ]

    if _renames:
        from boxyard._enums import RenameScope
        from boxyard.cmds._rename_box import rename_box
        from boxyard._models import refresh_boxyard_meta as _refresh

        # rename_box resolves the box through the cached meta, so make sure the
        # cache reflects what is actually on disk before renaming.
        _refresh(config)

        for _local_index_name, _remote_index_name in _renames:
            _new_name = _remote_index_name.split("__", 1)[1]
            if verbose:
                print(
                    f"Adopting remote name for '{_local_index_name}' "
                    f"-> '{_remote_index_name}' (renamed on another machine)."
                )
            await rename_box(
                config_path=config.config_path,
                box_index_name=_local_index_name,
                new_name=_new_name,
                scope=RenameScope.LOCAL,
                verbose=False,
            )
            # The local registration now matches the remote, so it is no longer
            # missing.
            _ls_local = {
                _p for _p in _ls_local
                if Path(_p).parts[0] != _local_index_name
            } | {f"{_remote_index_name}/{const.BOX_METAFILE_REL_PATH}"}

    missing_metas = sorted(_ls_remote - _ls_local)

    if box_index_names is not None:
        missing_metas = [
            missing_meta
            for missing_meta in missing_metas
            if Path(missing_meta).parts[0] in box_index_names
        ]

    if check_interrupted():
        raise SoftInterruption()

    missing_box_index_names = [Path(p).parts[0] for p in missing_metas]

    if len(missing_metas) > 0:
        if verbose:
            print(f"Syncing {len(missing_metas)} missing boxmetas from '{sl_name}'.")
            for missing_meta in missing_metas:
                print(f"  - {missing_meta}")

        await rclone_sync(
            rclone_config_path=config.rclone_config_path,
            source=sl_name,
            source_path=sl_config.store_path / const.REMOTE_BOXES_REL_PATH,
            dest="",
            dest_path=config.local_store_path / sl_name,
            filter=[f"+ /{p}" for p in missing_metas] + ["- **"],
            exclude=[],
        )

        # Create sync records
        async def _task(box_index_name):
            box_meta = BoxMeta.load(
                config, sl_name, box_index_name
            )  # Used to get the paths consistently
            rec = await SyncRecord.rclone_read(
                config.rclone_config_path,
                sl_name,
                box_meta.get_remote_sync_record_path(config, BoxPart.META),
            )
            await rec.rclone_save(
                config.rclone_config_path,
                "",
                box_meta.get_local_sync_record_path(config, BoxPart.META),
            )

        await async_throttler(
            [_task(box_index_name) for box_index_name in missing_box_index_names],
            max_concurrency=max_concurrent_rclone_ops,
        )
    else:
        if verbose:
            print(f"No missing boxmetas in '{sl_name}' to sync.")

# %% [markdown]
# Refresh the boxyard meta file

# %%
#|export
from boxyard._models import refresh_boxyard_meta

refresh_boxyard_meta(config)

# %%
#|func_return
missing_metas

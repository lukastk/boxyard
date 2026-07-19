# boxyard

A CLI tool for managing and syncing folders ("boxes") across local and remote storage using [rclone](https://rclone.org/). Track metadata, organize boxes into groups, and keep everything in sync with conflict detection.

## Install

```bash
pip install boxyard
# or
uv pip install boxyard
```

Requires [rclone](https://rclone.org/downloads/) to be installed and configured.

## Quick start

```bash
# Initialize boxyard (creates config and data directories)
boxyard init

# Create a new box from an existing folder
boxyard new --from ~/projects/my-project

# Sync a box to remote storage
boxyard sync --box-name my-project

# Check sync status
boxyard yard-status

# List all boxes
boxyard list
```

## What it does

Boxyard manages folders (called "boxes") that you want to keep synced between your local machine and remote storage (S3, SFTP, or any rclone-supported backend).

Each box has:
- **Data** (`data/`) - the actual folder contents
- **Metadata** (`boxmeta.toml`) - name, groups, storage location, creation info
- **Config** (`conf/`) - optional per-box configuration that controls how data is synced
- **Sync records** - track what's been synced and when, enabling conflict detection

### The `conf/` folder

Each box can optionally have a `conf/` folder containing rclone filter files that customize which files are included or excluded when syncing the box's data:

- `.rclone_include` - only sync files matching these patterns
- `.rclone_exclude` - skip files matching these patterns (if absent, the global default exclude list is used)
- `.rclone_filters` - combined include/exclude filter rules

During sync, the `conf/` folder is synced *before* the data, ensuring filter rules are up-to-date before they're applied. This means filter rules travel with the box across remotes -- if you want a box to always exclude `.venv/` or only include `*.csv`, put that in its `conf/` folder and it will apply everywhere the box is synced.

Boxes are identified by a unique ID (`{timestamp}_{subid}`, e.g. `20251122_143022_a7kx9`) and organized into groups via symlinks.

## Commands

| Command | Description |
|---------|-------------|
| `init` | Initialize boxyard config and data directories |
| `new` | Create a new box from a folder |
| `sync` | Sync a box with remote storage |
| `multi-sync` | Sync multiple boxes concurrently |
| `list` | List all boxes |
| `box-status` | Show sync status of a box |
| `yard-status` | Show sync status of all boxes |
| `doctor` | Read-only health check of the machine's boxyard state |
| `include` | Include a remote box in the local store |
| `exclude` | Exclude a box from the local store (keeps remote) |
| `delete` | Delete a box locally and/or remotely |
| `rename` | Rename a box locally, remotely, or both |
| `copy` | Copy a remote box to a local path without including it |
| `force-push` | Force push a local folder to a box's remote |
| `add-to-group` | Add a box to a group |
| `remove-from-group` | Remove a box from a group |
| `path` | Get the local path of a box |
| `which` | Identify which box a path belongs to |

### `doctor`

`boxyard doctor` runs a strictly read-only health check of the machine's boxyard state, so misuse and drift get caught mechanically. It never mutates or auto-fixes anything, and exits with code 0 when healthy and 1 when there is any finding — so it can run under cron/supervisors and be asserted by scripts and agents.

```bash
boxyard doctor                 # full check, including remote storage
boxyard doctor --no-remote     # offline: skip checks that access remote storage
boxyard doctor -o json         # machine-readable report
```

Checks:

| Check | What it flags |
|-------|---------------|
| `unregistered-folder` | Directories in `user_boxes_path` that are not registered boxes (e.g. hand-created instead of via `boxyard new`) |
| `malformed-name` | Entries in `user_boxes_path` whose names don't parse as `<timestamp>_<subid>__<name>` (legacy formats are accepted) |
| `broken-registration` | `local_store` registrations missing `boxmeta.toml`, or with one that fails to parse/validate |
| `duplicate-box-id` | The same box id registered more than once |
| `stale-cache` | `boxyard_meta.json` disagreeing with a fresh scan of `local_store` |
| `dangling-symlinks` | Group symlinks whose targets don't exist |
| `group-tree-debris` | Real (non-symlink) files in the group tree, which make `create-user-symlinks` raise |
| `orphaned-sync-records` | `sync_records/<index>/` with no matching registration |
| `interrupted-sync` | Local sync records left incomplete by an interrupted sync (the local copy may be incomplete), or that fail to parse |
| `unknown-storage-location` | `local_store` dirs and remote-index caches left over from removed/renamed storage locations |
| `rclone-config` | Unresolvable rclone binary, rclone storage locations with no remote in `boxyard_rclone.conf`, or a missing default exclude file |
| `stale-meta-mirror` | Remote boxmetas not mirrored locally (what `sync-missing-meta` would fetch); skipped with `--no-remote` |
| `tombstoned-box` | Locally registered boxes that were deleted (tombstoned) on the remote from another machine; skipped with `--no-remote` |
| `tree-orphans` | Boxmeta `parents` referencing unknown box ids |

Every finding comes with a one-line hint on how to fix it.

## Configuration

Config file: `~/.config/boxyard/config.toml`

```toml
default_storage_location = "my-remote"
boxyard_data_path = "~/.boxyard"
user_boxes_path = "~/boxes"
user_box_groups_path = "~/box-groups"

[storage_locations.my-remote]
storage_type = "rclone"
store_path = "boxyard"
```

Storage locations are defined as rclone remotes. Boxyard uses its own rclone config at `~/.config/boxyard/boxyard_rclone.conf`.

## Directory layout

```
~/.config/boxyard/
    config.toml              # Main config
    boxyard_rclone.conf      # rclone config for remotes

~/.boxyard/
    local_store/{remote}/    # Local copies of box data
    sync_records/            # Per-box sync state
    locks/                   # File locks for concurrent operations

~/boxes/                     # Symlinks to box data folders
~/box-groups/                # Group symlinks (e.g. ~/box-groups/work/my-project)
```

## Development

Boxyard uses [nblite](https://github.com/lukastk/nblite) for notebook-first development. Source files in `src/boxyard/` are autogenerated -- edit the `.pct.py` files in `pts/` instead.

```bash
uv sync                  # Install dependencies
nbl export --reverse     # Sync pts -> nbs (after editing .pct.py files)
nbl export               # Export nbs -> src/boxyard/
pytest src/tests/        # Run tests
```

## License

MIT

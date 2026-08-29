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
- **Machine-local placement** (`~/.boxyard/placements/`) - which named checkout root holds DATA on this machine; never synced

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
| `checkout-roots` | List configured roots and verified mount/device availability |
| `relocate` | Move an included checkout between roots locally, without remote I/O |

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
| `unregistered-folder` | Unregistered entries across every available checkout root |
| `malformed-name` | Entries in any checkout root whose names don't parse as `<timestamp>_<subid>__<name>` |
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
| `checkout-root-config` | Duplicate or overlapping root paths |
| `checkout-root-unavailable` | A guarded root whose exact mount target/device identity is absent or wrong, including boxes placed there |
| `checkout-placement` | Unknown roots, missing included checkouts, malformed/orphan placement records, or excluded boxes with DATA |
| `duplicate-checkout` | The same box physically present in multiple available roots |
| `interrupted-relocation` | A durable relocation transaction that needs `boxyard relocate -r BOX` recovery |

Every finding comes with a one-line hint on how to fix it.

## Configuration

Config file: `~/.config/boxyard/config.toml`

```toml
default_storage_location = "my-remote"
boxyard_data_path = "~/.boxyard"

# Permanent default checkout root, named "default". Existing configs need no migration.
user_boxes_path = "~/boxes"
user_box_groups_path = "~/box-groups"

[checkout_roots.bulk]
path = "~/large-boxes"

# Optional safety guard: both keys are required together. Boxyard verifies the
# exact mount target and stable filesystem UUID before touching this root.
[checkout_roots.volume]
path = "/mnt/volume/boxes"
mount_target = "/mnt/volume"
filesystem_uuid = "01234567-89ab-cdef-0123-456789abcdef"

[storage_locations.my-remote]
storage_type = "rclone"
store_path = "boxyard"
```

A **Boxyard catalog** is one namespace of identities, groups, parents, ownership and sync history. A remote **storage location** selects where a box is synced. A machine-local **checkout root** selects where that machine keeps the box's DATA. These are independent: one box may use remote `my-remote`, root `volume` on one machine, root `default` on another, and be excluded on a third. Placement never enters `boxmeta.toml`.

`user_boxes_path` is intentionally the permanent root named `default`; it is not a deprecated compatibility key. `[checkout_roots.*]` adds non-default roots. See [the checkout-root design](docs/checkout-roots.md).

Examples:

```bash
boxyard checkout-roots
boxyard new -n dataset --checkout-root bulk
boxyard include -r 20260101_abcde__dataset --checkout-root volume
boxyard relocate -r 20260101_abcde__dataset --checkout-root default
boxyard list --checkout-root volume --show-checkout
boxyard list -o json                 # includes checkout_root, local_path, state
boxyard which --path /mnt/volume/boxes/20260101_abcde__dataset -j
```

`exclude` remembers the root. A later `include` with no `--checkout-root` reuses it; an explicit root overrides it. Relocation is local-only, locked and recoverable. If interrupted, `doctor` reports it and rerunning `boxyard relocate -r BOX` resumes the recorded destination. Boxyard never falls back from an unavailable guarded root.

Storage locations are rclone remotes or local stores. Boxyard uses its own rclone config at `~/.config/boxyard/boxyard_rclone.conf`.

## Directory layout

```
~/.config/boxyard/
    config.toml              # Main config
    boxyard_rclone.conf      # rclone config for remotes

~/.boxyard/
    local_store/{remote}/    # Registration, boxmeta and per-box conf (not DATA)
    placements/{box_id}.json # Machine-local root preference/state/relocation
    sync_records/            # Per-box sync state
    locks/                   # File locks for concurrent operations

~/boxes/                     # DATA in the permanent "default" checkout root
<other roots>/               # DATA selected independently per box
~/box-groups/                # Group symlinks target authoritative DATA paths
```

## Development

Boxyard uses [nblite](https://github.com/lukastk/nblite) for notebook-first development. Source files in `src/boxyard/` are autogenerated -- edit the `.pct.py` files in `pts/` instead.

```bash
uv sync                  # Install dependencies
nbl export               # Export pts -> src/boxyard/
nbl clean                # Clean notebook outputs
pytest src/tests/        # Run tests
```

## License

MIT

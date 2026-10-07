# Discovery, configuration, and local/remote paths

Read this reference only for the relevant task. The [skill core](../SKILL.md)
contains authorization rules and routes to other bundled topics; examples here
are not permission to run mutating commands. Check `boxyard <command> --help`
for the installed CLI. Versioned observations and counts are historical, not
a live inventory.

## Configuration files and important paths

Default files and folders:

```text
~/.config/boxyard/config.toml          # main Boxyard config
~/.config/boxyard/boxyard_rclone.conf  # Boxyard's rclone config
~/.config/boxyard/default.rclone_exclude
~/.boxyard/                            # default boxyard_data_path
~/boxes/                               # default user_boxes_path; included box data appears here
~/box-groups/                          # default user_box_groups_path; group symlinks appear here
```

The config file controls the real locations. Important config keys:

```toml
default_storage_location = "..."
boxyard_data_path = "~/.boxyard"
user_boxes_path = "~/boxes"
user_box_groups_path = "~/box-groups"
max_concurrent_rclone_ops = 3

[checkout_roots.bulk]
path = "~/large-boxes"

# Optional guarded root (both keys required together)
[checkout_roots.volume]
path = "/mnt/volume/boxes"
mount_target = "/mnt/volume"
filesystem_uuid = "..."

[storage_locations.my-remote]
storage_type = "rclone" # or "local"
store_path = "boxyard"
```

Derived paths:

```text
<boxyard_data_path>/boxyard_meta.json          # cached local box index
<boxyard_data_path>/local_store/<storage>/     # local metadata/conf roots by storage location
<boxyard_data_path>/sync_records/              # local sync records
<boxyard_data_path>/sync_backups/              # local sync backups
<boxyard_data_path>/remote_indexes/            # cached remote index lookups
<boxyard_data_path>/placements/<box_id>.json # machine-local checkout placement
```

For a box with index name `<box_id>__<name>`:

```text
<configured-checkout-root>/<box_id>__<name>/                # authoritative local DATA path
<boxyard_data_path>/local_store/<storage>/<index>/           # local box root
<boxyard_data_path>/local_store/<storage>/<index>/boxmeta.toml
<boxyard_data_path>/local_store/<storage>/<index>/conf/
```

Remote/rclone stores use this layout under the storage location's `store_path`:

```text
boxes/<index>/data/          # plain boxes
boxes/<index>/data.restic/   # restic boxes: per-box repository (see "Storage format")
boxes/<index>/data.snapshot  # restic boxes: pointer to the current snapshot
boxes/<index>/boxmeta.toml
boxes/<index>/conf/
sync_records/<index>/<data|meta|conf>.rec
sync_backups/
```

The global CLI option for non-default config is:

```bash
boxyard --config /path/to/config.toml <command> ...
```

`boxyard init` uses `--config-path` and `--data-path` to create a config/data directory. The shell helper honors `BOXYARD_CONFIG_PATH`; normal Typer CLI commands should be given `--config` when using a non-default config.

`DEFAULT_BOX_GROUPS` can add default groups at runtime. It is parsed as a TOML list string, for example:

```bash
export DEFAULT_BOX_GROUPS='["ctx/mac", "work"]'
```

## How to find where boxes are

Use these patterns first.

### Find the box containing the current directory or any path

```bash
boxyard which
boxyard which --path /some/path
boxyard which --path /some/path --json
boxyard which --path /some/path --index-name
```

`which` searches every configured checkout root (including symlink-resolved paths) and reports the box name, id, index name, storage location, checkout root/state, authoritative local DATA path, and inclusion state.

### Get a box's data folder

```bash
boxyard path --box-name NAME --pick-first
boxyard path --box-id BOX_ID
boxyard path --box INDEX_NAME
```

`boxyard path` defaults to the **data** path. By default it filters to included boxes. Use `--all` when you need to select from included and excluded boxes.

### Get non-data paths for a box

```bash
boxyard path --box-name NAME --pick-first --path-option root
boxyard path --box-name NAME --pick-first --path-option meta
boxyard path --box-name NAME --pick-first --path-option conf
boxyard path --box-name NAME --pick-first --path-option sync-record-data
boxyard path --box-name NAME --pick-first --path-option sync-record-meta
boxyard path --box-name NAME --pick-first --path-option sync-record-conf
```

### Find checkout roots and actual local paths

Never reconstruct `user_boxes_path/<index_name>`: a box may be in any configured root. `user_boxes_path` is permanently the root named `default`; additional roots are `[checkout_roots.NAME]`. Use:

```bash
boxyard checkout-roots
boxyard list --show-status --show-checkout
boxyard list --checkout-root volume --output-format json
boxyard path --box INDEX_NAME
boxyard which --path /some/root/INDEX --json
```

Status markers are `●` included, `○` excluded, `!` root unavailable, `×` recorded checkout missing, and `↔` interrupted relocation. An unavailable root never falls back to default and its boxes remain in the catalog.

## Common discovery commands

```bash
boxyard list
boxyard list --show-status
boxyard list --output-format json
boxyard list --view groups --show-status
boxyard list --view tree --show-status
boxyard tree --show-status
boxyard list-groups --all --include-virtual
boxyard yard-status
```

Group filters support boolean expressions over group names:

```bash
boxyard list --group-filter 'work AND NOT archived'
boxyard path --group-filter 'ctx/mac OR ctx/linux' --pick-first
```

Other list filters:

```bash
boxyard list --include-group GROUP
boxyard list --exclude-group GROUP
boxyard list --children-of BOX
boxyard list --descendants-of BOX
boxyard list --parent-of BOX
boxyard list --ancestors-of BOX
boxyard list --roots
boxyard list --leaves
```

## Box selection options

Many commands accept one of:

```bash
--box INDEX_NAME       # full <box_id>__<name>
--box-id BOX_ID        # <timestamp>_<subid>
--box-name NAME        # defaults to contains matching for many commands
```

Name matching options:

```bash
--name-match-mode exact|contains|subsequence
--name-match-case
--pick-first           # available on `path`; use only when ambiguity is acceptable
```

With no `--box`/`--box-id`/`--box-name` at all, boxyard uses the box you are
standing in — anywhere under `<configured-checkout-root>/<index_name>/...`. If the cwd is
not inside a box (or the box is not a candidate for that command, e.g. an
already-included box for `include`), it falls back to an fzf picker over the
candidates. Commands that destroy something — `delete`, `rename`, `copy`,
`force-push`, `sync-name` — refuse a bare invocation outright and always need
an explicit selector.

## Canonical command discovery and optional source map

Use `boxyard --help`, `boxyard <command> --help`, and `boxyard --version` for the
installed CLI. These bundled references do not require a Boxyard source checkout.

If developing Boxyard, locate its checkout separately; **do not assume `../..`
from an installed skill is the repository root**. In that checkout, the former
source-reference list remains useful (paths below are checkout-relative, not
bundled skill dependencies):

- `README.md` — high-level usage and directory layout
- `src/boxyard/const.py` — defaults and constants
- `src/boxyard/config.py` — config model and derived paths
- `src/boxyard/_cli/app.py` — Typer app / package entry point
- `src/boxyard/_cli/main.py` — command definitions
- `src/boxyard/_cli/multi_sync.py` — `multi-sync`
- `src/boxyard/_models.py` — box path and metadata layout
- `src/boxyard/_shell_helper.py` — shell helper behavior

Generated source is for inspection only: edit `pts/mod/` and run `nbl export`
in the development checkout.

### Corrections to earlier examples

- Cwd selection uses all configured checkout roots, not just `user_boxes_path`.
- `max_concurrent_rclone_ops` is a top-level config key. The earlier example put
  it after `[checkout_roots.volume]`, which incorrectly nested it in that table.

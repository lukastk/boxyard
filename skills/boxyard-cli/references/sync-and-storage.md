# Sync, cadence, storage conversion, and excludes

Read this reference only for the relevant task. The [skill core](../SKILL.md)
contains authorization rules and routes to other bundled topics; examples here
are not permission to run mutating commands. Check `boxyard <command> --help`
for the installed CLI. Versioned observations and counts are historical, not
a live inventory.

## Syncing

Sync one box:

```bash
boxyard sync --box-name NAME
boxyard sync --box INDEX_NAME
boxyard sync --box-id BOX_ID
```

Sync only selected parts:

```bash
boxyard sync --box-name NAME --sync-choices meta
boxyard sync --box-name NAME --sync-choices conf
boxyard sync --box-name NAME --sync-choices data
```

Sync settings and direction:

```bash
boxyard sync --box-name NAME --sync-setting careful
boxyard sync --box-name NAME --sync-setting replace
boxyard sync --box-name NAME --sync-setting force
boxyard sync --box-name NAME --sync-direction push
boxyard sync --box-name NAME --sync-direction pull
```

Other sync commands:

```bash
boxyard multi-sync
boxyard multi-sync --storage-location STORAGE --max-concurrent 3
boxyard multi-sync --box INDEX_NAME --box OTHER_INDEX_NAME
boxyard multi-sync --due-only              # only boxes whose DATA cadence is due, most overdue first
boxyard multi-sync --skip-unchanged-meta   # META-only proof, for a --sync-choices meta pass
boxyard multi-sync --skip-unchanged        # prove all requested parts and dependencies unchanged (plain DATA too)
boxyard sync --box-name NAME --sync-children   # also sync every descendant box afterwards
boxyard sync-missing-meta
boxyard box-status --box-name NAME
boxyard yard-status
```

Soft interruption is enabled by default for long operations: interrupt once or twice to stop after the current operation; repeated interrupts exit immediately.

### Sync policies (cadence)

`[sync_policies.NAME]` tables in `config.toml` set, per box group, how often a box is due and what storage format new boxes get. Every field is optional; an unset field means "not stated here", not "off".

```toml
[sync_policies.default]        # the floor every box falls back to (its `groups` are ignored)
storage_format = "plain"

[sync_policies.cold]
groups = ["archived"]          # applies to boxes in any of these groups
data_interval = "7d"           # whole number + s|m|h|d|w
meta_interval = "1d"
```

Resolution is per setting: the box's own `conf/sync.toml` (may set only `data_interval`, `meta_interval`, `storage_format`) beats matching group policies, which beat `default`. Two matching policies stating different values for one setting is a conflict (`doctor`: `sync-policy-conflict`); an unparseable `conf/sync.toml` is `unusable-box-sync-conf`. With no policies at all every box is always due, so `--due-only` changes nothing. `--due-only` selects on `data_interval`, measured from the last successful check that `multi-sync` records machine-locally under `<boxyard_data_path>/sync_checks/<index>/<part>.json`.

## Storage format (plain vs restic) and `convert`

A box's DATA is stored either as a **plain** rclone tree (`boxes/<index>/data/`) or as a per-box **restic** repository (`boxes/<index>/data.restic/`, plus a `boxes/<index>/data.snapshot` pointer naming the current snapshot). META and CONF are always plain. The format is recorded in the boxmeta as `storage_format` and is a fact about the box: it is stamped once at `new` from the resolved sync policy, and afterwards only `boxyard convert` changes it — a config edit never reformats existing boxes.

When no policy states a format, the **package default is `restic`** for rclone storage locations (`plain` for `local` ones). Lukas's rig pins `[sync_policies.default] storage_format = "plain"`, so his new boxes are plain; a config without that pin creates restic boxes, and `new` refuses to create one when no restic password is configured.

**Plain is right for almost every box.** Before recommending or changing a format, read the rig's current policy at `~/.config/myagent/reference/box-storage.md` (the former global AGENTS.md "Box storage format" section moved there). The rule remains: convert only for more than ~5,000 **directories** (directory count, not file count, drives plain sync cost), for >~1 GB of large files that get rewritten or re-snapshotted, or when encryption at rest / snapshot history is wanted.

```bash
boxyard convert -r INDEX_NAME --dry-run                   # read-only: local shape (counts excluded paths too)
boxyard convert -r INDEX_NAME --dry-run --estimate-size   # also measure what restic would store (reads the whole box, writes nothing remote)
boxyard convert -r INDEX_NAME                             # plain -> restic (prompts; -y skips)
boxyard convert -r INDEX_NAME --to-plain                  # restic -> plain
```

`convert` verifies a byte-identical restore (content, mode, symlinks) before the old copy is removed, and is resumable after an interruption. It refuses — before writing anything — for a box in a `local` storage location, a box not checked out on this machine, a box whose sync lock is held, or one with an interrupted DATA sync. **A machine on boxyard older than 0.7.0 cannot read a restic box**, so check `ssh-target <machine> boxyard --version` across the fleet first.

Restic boxes need, on every machine: the `restic` binary (`BOXYARD_RESTIC` points at an explicit one), and the repository password from `$BOXYARD_RESTIC_PASSWORD` or the config's `restic_password_command` (the rig uses `secret get BOXYARD_RESTIC_PASSWORD`). Machine-local state lives in `<boxyard_data_path>/restic_state/` (never synced), and backups go through the fixed symlink root `/tmp/boxyard-restic` so every machine records the same snapshot path. `doctor` reports `storage-format-mismatch` and `orphaned-snapshot`.


## Per-box sync configuration

Each box can have a `conf/` folder. Boxyard syncs `conf/` before `data/`, so filters travel with the box.

Special files:

```text
conf/.rclone_exclude  # exclude matching files (REPLACES the default list)
conf/sync.toml        # per-box sync policy override (see "Sync policies")
```

`conf/.rclone_include` and `conf/.rclone_filters` are recognised but currently **REFUSED**: an exclude file always applies, and boxyard will not combine rclone filter families (rclone applies them in an indeterminate order). A box that has either file fails its DATA sync until boxyard merges the three into one ordered filter list (ticket 43f05498). Express a box's scope in `conf/.rclone_exclude` alone.

If `conf/.rclone_exclude` is absent, Boxyard uses:

```text
~/.config/boxyard/default.rclone_exclude
```

If `conf/.rclone_exclude` exists it **REPLACES** `~/.config/boxyard/default.rclone_exclude` entirely — it does not extend it. So copy the default file's contents in and append to it; a one-line `.rclone_exclude` would start syncing `.venv/`, `node_modules/` and the like. The package default (written by `init`) is `.venv/`, `.pixi/`, `.trunk/`, `node_modules/`, `__pycache__/`, and `.DS_Store`; Lukas's rig deploys a longer list (36 entries).

## Skip-proof correction and exclude hazards

The earlier skill described `--skip-unchanged` as META plus restic DATA only,
with plain DATA never skipped. That predates the v0.8.4 full-pass proof: requested
parts and their dependencies must all be provably unchanged. A bulk hashed listing
provides remote record md5s, compared to identities previously agreed by this
machine; local trees must match record-bound fingerprint baselines. Restic DATA
retains its snapshot-pointer check. An unprovable box runs the real sync path.
No skip markers are written remotely and identities are not blessed from listings.

Changing the default exclude file invalidates DATA baselines fleet-wide. Batch
needed changes into one edit rather than paying for repeated full passes. Keep
large generated outputs inside their related box; exclude them with the box's
filter instead of creating sibling directories to dodge sync.

# Creation, placement, groups, and destructive operations

Read this reference only for the relevant task. The [skill core](../SKILL.md)
contains authorization rules and routes to other bundled topics; examples here
are not permission to run mutating commands. Check `boxyard <command> --help`
for the installed CLI. Versioned observations and counts are historical, not
a live inventory.

# Boxyard CLI Skill

Use this skill when the user wants to **use** Boxyard, not develop Boxyard itself.

Boxyard is a Python CLI for managing and syncing folders ("boxes") across local and remote storage using rclone or local storage. A box has data, metadata, optional sync configuration, group membership, and sync records.

## Where generated data should live

Generated data — including large outputs — generally belongs **inside** the box it relates to, not in some sibling directory chosen to dodge syncing. Do not move heavy outputs out of a box to keep it "small". The whole point of Boxyard is that syncing makes large boxes comfortable to live with: anything you don't want pushed can be excluded from sync via the box's `conf/.rclone_exclude` (see "Per-box sync configuration"), or the box itself can be excluded locally with `boxyard exclude`. Keep data colocated with its box and control sync with filters — don't fragment it to avoid sync.

## Before running commands

- If operating from this repository checkout, prefer:

  ```bash
  cd /path/to/boxyard && uv run boxyard ...
  ```

  If Boxyard is already installed in the environment, `boxyard ...` is also fine.

- Read-only commands are safe to run without confirmation: `--help`, `list`, `tree`, `path`, `which`, `box-status`, `yard-status`, `list-groups`, `owner`, `doctor`, `convert --dry-run`.
- Ask before running commands that can modify local or remote state: `init`, `new`, `sync`, `multi-sync`, `sync-missing-meta`, `include`, `exclude`, `delete`, `rename`, `sync-name`, `add-to-group`, `remove-from-group`, `add-parent`, `remove-parent`, `create-user-symlinks`, `copy`, `force-push`, `claim`, `release`, `discard-local`, `convert`.
- Be especially careful with:
  - `boxyard new --from PATH` / `-f PATH`: moves `PATH` into Boxyard unless `--copy` is supplied.
  - `boxyard exclude`: syncs first by default, then removes the local data copy.
  - `boxyard delete`: deletes a box.
  - `boxyard sync --sync-setting replace|force`: can overwrite data depending on direction/status.
  - `boxyard force-push --force`: destructively overwrites remote data from a local source folder.


## Creating boxes

Create an empty box:

```bash
boxyard new --box-name NAME
```

Create from an existing folder, moving the folder into Boxyard:

```bash
boxyard new --from /path/to/folder
```

Copy from an existing folder instead of moving it:

```bash
boxyard new --from /path/to/folder --copy
```

Clone a git repo as a new box:

```bash
boxyard new --git-clone git@github.com:user/repo.git
```

Useful options:

```bash
boxyard new --box-name NAME --storage-location STORAGE
boxyard new --box-name NAME --group GROUP --group OTHER_GROUP
boxyard new --box-name NAME --parent PARENT_BOX
boxyard new --box-name NAME --no-initialise-git
boxyard new --box-name NAME --no-claim   # don't make THIS machine the write owner
```

`new` claims the box for the creating machine by default (see "Write ownership"). Pass `--no-claim` when creating a box here that will be worked on elsewhere, or the other machine's pushes will be refused.

A new box's storage format (`plain` or `restic`) is fixed at creation from the resolved sync policy — see "Storage format (plain vs restic) and `convert`".

Select local placement independently of remote storage:

```bash
boxyard new --box-name NAME --storage-location STORAGE --checkout-root ROOT
boxyard include --box-name NAME --checkout-root ROOT
boxyard relocate --box-name NAME --checkout-root OTHER_ROOT
boxyard relocate --box-name NAME --checkout-root ROOT --adopt-existing
boxyard relocate --box-name NAME  # recover the recorded destination after interruption
```

`exclude` remembers the preferred root; `include` without a root reuses it. `relocate` is locked, local-only, does no remote I/O, and is recoverable via `doctor`. Use `--adopt-existing` only for a pre-populated destination: Boxyard verifies every source entry is identical there, preserves destination-only content, then commits placement and removes the source.


## Include, exclude, copy

Include an excluded remote box locally:

```bash
boxyard include --box-name NAME
boxyard include --interactive
```

Exclude a local copy while keeping the remote:

```bash
boxyard exclude --box-name NAME
boxyard exclude --interactive --show-sizes
boxyard exclude --box-name NAME --skip-sync
```

Copy a remote box to an arbitrary destination without adding it to Boxyard tracking:

```bash
boxyard copy --box-name NAME --dest ./NAME-copy
boxyard copy --box-name NAME --dest ./NAME-copy --meta --conf
boxyard copy --box-name NAME --dest ./NAME-copy --overwrite
```

## Groups and hierarchy

Groups:

```bash
boxyard add-to-group --box-name NAME GROUP [OTHER_GROUP ...]
boxyard remove-from-group --box-name NAME GROUP [OTHER_GROUP ...]
boxyard list-groups --box INDEX_NAME
boxyard list-groups --all --include-virtual
boxyard create-user-symlinks
```

Parent-child hierarchy:

```bash
boxyard add-parent --box-name CHILD --parent-name PARENT
boxyard remove-parent --box-name CHILD --parent-name PARENT
boxyard tree --show-status
boxyard list --view tree --show-status
```

## Rename, delete, and force operations

Rename:

```bash
boxyard rename --box-name OLD --new-name NEW --scope both
boxyard rename --box-name OLD --new-name NEW --scope local
boxyard rename --box-name OLD --new-name NEW --scope remote
```

Sync only the name between local and remote:

```bash
boxyard sync-name --box-name NAME --to-local
boxyard sync-name --box-name NAME --to-remote
```

Delete:

```bash
boxyard delete --box-name NAME
boxyard delete --box-name NAME --force   # needed when the box has children
```

Destructive force push:

```bash
boxyard force-push --box-name NAME --source /path/to/source --force
```

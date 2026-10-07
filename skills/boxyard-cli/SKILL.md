---
name: boxyard-cli
description: Use the Boxyard CLI to manage, find, inspect, sync, include, exclude, group, rename, or copy boxes. Use when the user asks about boxyard command usage, Boxyard config files, locating box folders, rclone-backed storage, sync status, or shell/TUI helpers.
---

# Boxyard CLI

Use this skill to **use**, not develop, Boxyard: managed folders with DATA,
metadata, sync config, groups, and local/rclone storage. All bundled paths below
resolve from this skill directory; no source checkout is required.

## Authority and safety

- **Ask before modifying local or remote state.** This includes `init`, `new`,
  `sync`, `multi-sync`, `sync-missing-meta`, `include`, `exclude`, `relocate`,
  `delete`, `rename`, `sync-name`, group/parent edits, `create-user-symlinks`,
  `copy`, `force-push`, `claim`, `release`, `discard-local`, and non-dry-run `convert`.
  Examples are not authorization. Read-only queries, `--help`, `doctor`, and
  `convert --dry-run` may run without confirmation.
- **Never create ad-hoc folders/clones/worktrees in `~/dev`.** Find the existing
  box or use `boxyard new` with approval; put non-box scratch outside managed roots.
  Keep generated data, even large outputs, **inside its related box**. Use sync
  excludes, not sibling folders, to avoid uploading it.
- **Resolve identity and placement, don't guess.** Both timestamp format and subid
  length are configurable; `index_name` is `{box_id}__{name}`. Use `which`/`path`:
  DATA may be in any checkout root, not just `user_boxes_path` (`default`). An
  unavailable root never falls back to default. Placement is machine-local, never
  synced in `boxmeta.toml`; remote storage and local placement are independent.
- **Resolve ownership, never bypass it.** DATA/CONF pushes compare `write_owner`
  with configured `machine_name`, never a hostname. No owner means unrestricted.
  `new` claims by default (`--no-claim` for work elsewhere); `include --read-only`
  suppresses the claim nudge. Refusal also protects `force-push`, remote/both rename,
  and deletion. With approval, `claim --steal` keeps this machine's work;
  `discard-local` replaces it from remote and keeps overwritten work in backups.
- **Destructive traps:** `new --from PATH` MOVES unless `--copy`; `exclude` syncs
  first then removes local DATA (`--skip-sync` skips that protection).
  `delete` tombstones the id fleet-wide: **never delete a stale duplicate name**
  to clean registrations. `sync --sync-setting replace|force`, `copy --overwrite`,
  and `force-push --force` can overwrite data. Read the relevant reference first.
- **Backups aren't garbage:** orphan findings may be intentional `discard-local`
  keepsakes. Never auto-delete them or infer disposability from unclaimed ULIDs.
- **Excludes replace, not extend.** A box's `conf/.rclone_exclude` replaces the
  default list entirely: copy the default contents before adding rules, or risk
  syncing `.venv/`, `node_modules/`, etc. `.rclone_include` and `.rclone_filters`
  are currently refused; use `.rclone_exclude` alone. CONF syncs before DATA.
  Default-exclude edits invalidate DATA baselines fleet-wide; batch needed edits.
- **Conversion is explicit:** policy edits never reformat existing boxes; only
  `convert` does, verifying a byte-identical restore before removing the old copy.
  Read the storage reference and `~/.config/myagent/reference/box-storage.md`
  before recommending/changing plain vs restic. Rig default is plain; package
  default for rclone is restic (local is plain). Every participating machine needs
  Boxyard >=0.7.0, restic and the password before using restic boxes.

## Discover, select, inspect

Use the installed CLI as authority: `boxyard --help`, `boxyard <command> --help`,
`boxyard --version`. When already operating from a Boxyard development checkout,
prefer `uv run boxyard ...`; do not navigate relative to the installed skill to
find source. For non-default config use `boxyard --config /path/to/config.toml ...`.
`BOXYARD_CONFIG_PATH` is for the shell helper, not a substitute for CLI `--config`.

```bash
boxyard which --path /some/path --json
boxyard list --output-format json
boxyard list --show-status --show-checkout
boxyard list --group-filter 'work AND NOT archived'
boxyard checkout-roots
boxyard path --box INDEX_NAME
boxyard path --box INDEX_NAME --path-option conf
boxyard box-status --box INDEX_NAME
boxyard owner --box INDEX_NAME
boxyard yard-status
boxyard doctor --no-remote
```

- `path` defaults to included DATA; `--all` includes excluded candidates. Use its
  `root`, `meta`, `conf`, or `sync-record-*` options for non-DATA locations.
- Select explicitly with `--box INDEX_NAME` or `--box-id BOX_ID`; `--box-name NAME`
  often means contains matching. `--pick-first` on `path` is only for acceptable
  ambiguity. Bare selection uses the cwd box across configured roots, then an
  fzf picker; `delete`, `rename`, `copy`, `force-push`, and `sync-name` require an
  explicit selector.
- **Run `doctor` when state looks inconsistent**, rather than improvising repairs.
  It is read-only (exit 0 healthy / 1 findings); default includes remote checks,
  `--no-remote` is offline. Follow hints only after approval for state changes.

## Common operations (after approval)

```bash
boxyard new --box-name NAME
boxyard new --from /path/to/folder --copy
boxyard include --box INDEX_NAME --checkout-root ROOT
boxyard sync --box INDEX_NAME --sync-setting careful
boxyard exclude --box INDEX_NAME
boxyard add-to-group --box INDEX_NAME GROUP
```

`sync` can select META/CONF/DATA, direction, and descendants. `multi-sync` supports
cadence and skip proofs; proof must cover all requested parts and dependencies,
including plain DATA. Unknown/unproven boxes still run the real sync path.
For relocation, destructive recovery, conversion, or bulk operations, read the
matching reference and command help before acting.

## Task-to-reference map (load only what is needed)

| Task | Bundled reference |
| --- | --- |
| Config keys, local/remote layouts, roots, selectors, filters, canonical discovery | [Discovery and paths](references/discovery-and-paths.md) |
| New/include/exclude/copy, relocation, groups/hierarchy, rename/delete/force | [Box lifecycle](references/box-lifecycle.md) |
| Sync modes, multi-sync proofs, cadence, plain/restic conversion, excludes | [Sync and storage](references/sync-and-storage.md) |
| Owner/claim/release/discard-local and doctor findings/recovery hints | [Ownership and health](references/ownership-and-health.md) |
| Ctrl+G, fzf, helper search and zsh setup | [Shell helper](references/shell-helper.md) |

Package-default paths are not rig paths: read the actual config (normally
`~/.config/boxyard/config.toml`) and use `path`. References preserve detailed
examples and rationale; historical counts are not a current fleet inventory.

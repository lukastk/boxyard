# Boxyard operating guide

Boxyard is Lukas's Python CLI for managing folders ("boxes"), groups, metadata,
and bidirectional local/rclone sync with conflict detection. Fleet configuration
belongs to myrig; this repository owns the CLI and sync implementation.

## Source of truth and commands

- **Edit `pts/mod/` and `pts/tests/` (`.pct.py` percent-format Python), then run
  `nbl export`. Never hand-edit `src/boxyard/` or generated `src/tests/`.**
- `nblite.toml` maps `pts/mod/` → `src/boxyard/`, `pts/tests/` → `src/tests/`.
  `nbs/` was removed and is ignored; there is no `.ipynb` round-trip. The vestigial
  `nbs -> pts` pipeline legs do not change this rule.
- Generated package map: `_cli/` = Typer entry/commands, `cmds/` = implementations,
  `_models.py` = metadata/records, `config.py` = config, `_utils/` = transport/helpers.
  See the development reference for the full module map and cell directives.
- `docs/` holds task references; `_dev/` holds designs and experiments.

```bash
uv sync                  # Install dependencies to .venv
direnv allow             # Auto-load environment
nbl install-hooks        # Install notebook-validation hooks
nbl export               # Regenerate package and tests after .pct.py edits
nbl clean                # Clean notebook outputs
pytest src/tests/        # Full test suite
pytest src/tests/unit/config/test_config_loading.py -v  # Targeted example
nbl test                 # Check notebooks execute
```

Use task-appropriate validation; documentation-only work needs link/diff checks,
not real box syncs or metadata changes.

**Pushing `main` IS a fleet release:** myrig installs from git, not PyPI.
PyPI releases use `dev_scripts/publish-new` and the tag-triggered workflow;
**never run `uv publish` by hand**. Read the release reference before release work.

## Mandatory safety and sync invariants

- **Identity:** both halves of `box_id` are config-driven; never hardcode its shape.
  `index_name` is `{box_id}__{name}`. Reconcile remote renames by id. **Never
  `delete` a stale duplicate name**: it tombstones the live id fleet-wide.
- **Ownership:** DATA/CONF pushes use `may_push`/`owner_gate`; refusal raises
  `OwnershipRefused`. `write_owner is None` is unrestricted, not denied.
  Compare with configured `config.machine_name`, never a hostname; when configured,
  `known_machines` must contain it. Deleting an already remotely tombstoned ghost
  skips the gate, not normal deletion.
- **Storage/placement:** `BoxMeta.storage_format` records what a box HAS, stamped
  by `new`; only `boxyard convert` changes it, with verified byte-identical restore
  before removing the old copy. META/CONF remain plain. Package rclone default is
  restic, local default plain; Lukas's rig pins plain. Checkout placements are
  machine-local under `~/.boxyard/placements/`, **never synced in `boxmeta.toml`**;
  `user_boxes_path` permanently names the `default` root.
- **Policies:** resolve each dimension: box `conf/sync.toml` > matching group
  policies > default. Conflicting group values raise `PolicyConflict`; no policies
  means always due, not silently skipped.
- **Push ordering:** write `<part>.inflight.json` with the incomplete ULID BEFORE
  the remote incomplete record, then write local. A matching remote incomplete
  record is our interrupted push. Local-first looks like an interrupted pull and
  can overwrite unpushed work.
- **Comparison/blessing:** use `_utils.rclone_would_transfer` dry-run sync/copyto,
  **never `rclone check`**; pin JSON logging flags, ignore modtime-only changes.
  First baselines require a no-transfer `verify_then_bless` probe. Push baselines
  use the PRE-transfer tree. Directory pulls fingerprint after transfer and bless
  only if a dry-run pull moves nothing; timestamp/ctime gates cannot detect the
  right races. Single-file pulls compare mtime against pull start.
- **Fingerprints:** sidecars are machine-local, ULID-bound, never synced; pulled
  records stay verbatim (`SyncRecord` forbids extras). Writer and reader must use
  `BoxMeta.get_effective_exclude_path` for DATA, `None` for META/CONF. The tree digest
  includes transport-enumerated entries, exclude signature and owner-exec bit.
  META/CONF convergence blesses on SYNCED; DATA verifies against remote first.
- **UNKNOWN is no claim, not changed.** Missing/unusable baselines retain the
  newest-mtime fallback. Its v0.8.7 removal was reverted despite the old gate being
  met: recovery, convergence and skip paths need to reach SYNCED to probe/bless.
  Do not treat that gate as cleanup authorization, or replace UNKNOWN with `False`
  without a design decision. Restic adoption of a plain baseline instead refuses
  loudly; doctor's coverage excludes that case and cannot license a fallback.
- **Skip proofs:** skip only when EVERY part in the executed closure (DATA includes
  META+CONF) is proven unchanged: remote record md5 matches the locally agreed
  identity sidecar; held parts also have complete local records with matching ULID
  and fingerprint. Do not stamp identity from listings or write remote skip markers.
- **Excludes/perms:** changing the default exclude list invalidates every DATA
  baseline fleet-wide; batch all needed lines into ONE edit and expect churn.
  `.boxyard-perms.json` is generated before push/applied after pull; v1 restores
  `+x` only, never clears it.
- **Backups:** `sync_backups/<ULID>/` is human recovery storage, never automatically
  read back. Purge failures must raise. `discard-local` deliberately keeps backups;
  doctor's orphan findings cannot distinguish these from failed purges. **Never
  auto-delete them or infer disposability from absence of a claiming sync record.**

## Read only the reference relevant to the task

| Task | Reference |
| --- | --- |
| Percent cells, function export, module/API map | [Development and architecture](docs/development-reference.md#architecture) |
| Publishing or fleet installation | [Deployment and releases](docs/development-reference.md#deployment-and-releases) |
| Config locations and local paths | [Configuration paths](docs/development-reference.md#configuration-paths) |
| Remote identity, skip proofs, interrupted push/pull | [Skip and ordering](docs/sync-invariants.md#skip-proofs-and-transfer-ordering) |
| Ownership, tombstones, duplicate registrations | [Ownership](docs/sync-invariants.md#ownership-and-stale-registrations) |
| Restic/plain conversion, policies, permissions | [Storage details](docs/sync-invariants.md#storage-policies-placement-and-permissions) |
| Fingerprints, missing baselines, fallback cleanup | [Fingerprint rationale and reversal history](docs/sync-invariants.md#fingerprints-and-the-reverted-fallback-cleanup) |
| Changing default excludes | [Fleet impact history](docs/sync-invariants.md#exclude-list-fleet-impact) |
| Doctor orphan backups or recovery | [Backup semantics and incident history](docs/sync-invariants.md#backups-and-recovery-history) |
| Checkout placement/relocation | [Checkout roots](docs/checkout-roots.md) |

Detailed sync references link the full skip/restic design notes. Dated incidents
and counts there are historical, not current fleet measurements. Local personal
memory, when present, belongs in ignored `AGENTS.local.md`; keep it compact and
never move private notes into tracked references.

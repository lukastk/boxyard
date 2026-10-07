# Sync invariants and operational rationale

On-demand detail for sync, ownership, storage, and recovery changes. The concise
mandatory guardrails remain in [the operating guide](../AGENTS.md). Paths in code
spans are repository-relative unless absolute or home-relative.

## Reading the history

The versioned and dated entries below preserve the original engineering record.
Incident counts, fleet sizes, performance timings, and diagnoses describe the
recorded events, not a fresh measurement of the fleet. In particular, the v0.8.7
fallback-removal reversal and 2026-09-28 gate review are history with an unresolved
design question, not permission to retry the cleanup. Verify current code before
changing behaviour.

## Identity and records

- **box_id**: `{timestamp}_{subid}` — BOTH halves are config-driven, so do not
  hardcode a shape. `box_timestamp_format` is either `date_only` (`%Y%m%d`) or
  `date_and_time` (`%Y%m%d_%H%M%S`); `box_subid_length` defaults to 5
  (`DEFAULT_BOX_SUBID_LENGTH`). Lukas's rig sets `date_only` + length 6, so real
  boxes look like `20260222_5xt51m` — matching the package defaults instead would
  never match one of his boxes.
- **index_name**: `{box_id}__{name}` - unique identifier for each box
- **Storage locations**: local filesystem or rclone remotes (S3, SFTP, etc.)
- **Sync records**: Track sync state between local/remote in `~/.boxyard/sync_records/`
## Skip proofs and transfer ordering

- **Remote record identity (v0.8.4)**: `multi-sync --skip-unchanged`
  (`_sync_policy.boxes_needing_sync_full`) drops a box from a pass only when
  every part in the closure of what it would execute (`closure(DATA) = {META,
  CONF, DATA}`) is provably unchanged: the remote record's md5 -- from ONE
  `lsjson --hash` over `sync_records/` (~2.5 min on the storage box; hashing
  is serial there) -- equals the md5 in the machine-local sidecar
  `sync_records/<box>/<part>.remote.json` (`_remote_identity`), which the real
  path writes when it last agreed with that record (SYNCED / EXCLUDED verdict,
  completed push or pull); a held part's local record must be complete and
  name the sidecar's ULID, and its tree must match the fingerprint baseline.
  Boxes are matched to the `boxes/` listing by id (`remote_view_for`), so a
  box renamed elsewhere is judged under its remote name. NOTHING is written to
  the remote for this and nothing is stamped from a listing; the first pass
  after an upgrade is a full pass that writes the sidecars. Design and its
  review history (v3/v4 markers withdrawn): `_dev/FULL-PASS-SKIP-DESIGN-NOTE.md`.
- **Dry-run comparison, never `rclone check`**: "would a push/pull move
  anything" is answered by `_utils.rclone_would_transfer` (a `--dry-run` sync,
  or `copyto` for a single file, with `--use-json-log` and every logging flag
  pinned on the command line; modtime-only differences do not count). Every
  first baseline for DATA, CONF and META is blessed only after it says nothing
  would move (`_fingerprint.verify_then_bless`). `rclone check` was removed:
  it hashed every file, one remote exec each on the SFTP box.
- **In-flight push sidecar**: a push writes `<part>.inflight.json` (the
  incomplete record's ULID) BEFORE its remote record write, remote first, then
  local. A remote incomplete record whose ULID the sidecar names is this
  machine's own interrupted push and is retried; never write the local
  incomplete record first — a local-only incomplete record reads as an
  interrupted pull and the retry pulls over unpushed work.
- **Pull blessing**: a directory pull fingerprints the post-transfer tree, then
  records that fingerprint as the baseline only if a dry-run pull would move
  nothing (a racing local write, deletion or rename during the transfer must not
  be blessed); a single-file pull uses the file's mtime against the pull start.
  A timestamp/ctime gate cannot do this job — the pull's own writes trip it.
## Ownership and stale registrations

- **Write ownership (single-writer, v0.5.2)**: `BoxMeta.write_owner` names the one
  machine allowed to push a box's DATA/CONF. `write_owner is None` means UNOWNED and is
  fully unrestricted — exactly the pre-feature behaviour — so ownership is opt-in per box
  and never silently blocks an unclaimed box. The owner is compared against
  `config.machine_name`, which is **configured, never derived from the hostname**.
  Commands: `claim`, `release`, `owner`, and `discard-local`; enforcement is
  `may_push()` plus `owner_gate()`, and refusal raises `OwnershipRefused`.
  `delete` of a box the remote already holds a tombstone for skips the gate
  (v0.8.5): it removes a ghost, not the shared copy. Config `known_machines`
  (v0.8.6, rendered by myrig from its `machines` table) lists every name that
  may own a box; with it `doctor`'s `stale-owner` is exact, without it the
  check guesses from owner counts. `machine_name` must be in the list.
- **Stale duplicate registrations (v0.8.6)**: `sync-missing-meta` reconciles
  on box id, adopts a remote rename, and drops a registration under a name the
  remote no longer has when the remote's name is also registered here and the
  stale one holds no DATA (checkout EXCLUDED or MISSING). Never `delete` a
  stale name: that tombstones the id, i.e. the live box, fleet-wide.
## Storage, policies, placement, and permissions

- **Storage format (v0.7.0+)**: a box's DATA is either a plain rclone tree
  (`boxes/<index>/data/`) or a per-box restic repository
  (`boxes/<index>/data.restic/` + `data.snapshot` pointer); META/CONF are always
  plain. `BoxMeta.storage_format` records what a box HAS and is stamped once by
  `new` from the resolved policy; only `boxyard convert` changes it, and `doctor`
  reports `storage-format-mismatch` / `orphaned-snapshot`. The PACKAGE default for
  rclone storage is `restic` (`_sync_policy.DEFAULT_STORAGE_FORMAT`; `local` →
  plain), and Lukas's rig pins `[sync_policies.default] storage_format = "plain"`.
  Needs the `restic` binary (`BOXYARD_RESTIC`) and a password from
  `BOXYARD_RESTIC_PASSWORD` or config `restic_password_command`. Design:
  `_dev/RESTIC-DATA-STORAGE-DESIGN-NOTE.md`.
- **Sync policies**: `[sync_policies.NAME]` (`data_interval`, `meta_interval`,
  `storage_format`, `groups`) resolved per dimension — a box's own
  `conf/sync.toml` beats group policies, which beat `default`; two matching
  policies with different values raise `PolicyConflict` (`doctor`:
  `sync-policy-conflict`, `unusable-box-sync-conf`). With no policies every box
  is always due, keeping un-opted-in configs unchanged.
- **Checkout roots**: machine-local DATA placement, independent of remote storage. `user_boxes_path` is permanently the root named `default`; additional roots are `[checkout_roots.NAME]`. Placement records live under `~/.boxyard/placements/` and must never be added to synced `boxmeta.toml`. See `docs/checkout-roots.md`.
- **Exec-bit manifest**: `.boxyard-perms.json` at a box's DATA root records which
  files are executable, so `+x` survives sync over backends that drop Unix mode
  (e.g. SFTP). Generated before push / applied after pull by `_utils/perms.py`.
  v1 is additive-only (restores `+x`, never clears it).
## Fingerprints and the reverted fallback cleanup

- **Fingerprint baselines (v0.8.0, transport-enumerated since v0.8.3)**: "has
  this box changed locally?" is answered by comparing a digest of the tree —
  enumerated by `rclone lsf` under the box's REAL exclude file, per entry
  `(relpath, kind, size-or-symlink-target, rclone's max-precision mtime,
  owner-exec bit)`, plus a signature of the exclude rules — against a
  machine-local sidecar `sync_records/<box>/<part>.base.json` written when a
  sync completes (push: the PRE-transfer tree; pull: the post-pull tree,
  refused if anything changed while the pull ran), on the non-owner
  probe-clean path, and by the convergence paths (META/CONF bless on a SYNCED
  verdict; DATA verifies against the remote first). The sidecar is bound to
  the sync record's ULID and NEVER synced (a pulled record is the pusher's
  verbatim, and `SyncRecord` is `extra="forbid"`). No usable baseline (absent,
  wrong `FINGERPRINT_VERSION`, ULID mismatch, or filter-signature mismatch)
  falls back to the old newest-mtime test — the `TODO(cleanup)` sites in
  `_models` and `_doctor`. **That removal was attempted in v0.8.7 and reverted,
  and the TODOs now record why.** Its stated gate (0 uncovered baselines on every
  machine, plus a deliberate backlog review) was genuinely met on 2026-09-28, but
  the gate was written at v0.8.0 and three mechanisms added after it — baseline
  convergence (v0.8.3), interrupted-sync/mid-transfer recovery, and the full-pass
  skip (v0.8.4) — all require UNKNOWN to mean "no claim" rather than "changed": a
  baseline-less part must be able to reach a SYNCED verdict, because that verdict
  is what triggers the bless or the verify-then-bless probe, and the probe is what
  surfaces a hidden divergence as a warning instead of pushing over it. 12
  integration tests say so. What is still open is whether the UNKNOWN branch can
  become a plain `False` and let the probe decide everything; that is not free
  either (META/CONF bless without verifying), so it is a decision, not a cleanup.
  `_restic_sync`'s adoption check is the ONE site that refuses loudly instead of
  falling back, and for a structural reason: it reads the PLAIN baseline of a box
  that is now restic, which is exactly the part doctor's coverage number skips, so
  that number can never license a fallback there. Signatures must match between
  writer and reader:
  resolve the exclude with `BoxMeta.get_effective_exclude_path` for DATA and
  pass None for META/CONF — anything else makes the baseline dead weight.
## Exclude-list fleet impact

- **Editing the default exclude list is a fleet-wide event, not a config tweak.**
  The exclude rules are hashed into every baseline, so adding one line to
  `~/.config/boxyard/default.rclone_exclude` (rendered from myrig) invalidates
  every DATA baseline on every machine at once — ~596 boxes x 5 — and each one
  then reads NEEDS_PUSH until its next sync rewrites the baseline. Nothing is
  lost (a push that moves nothing still re-records), but it is hours of fleet
  churn and a `fingerprint-baseline-missing` finding everywhere until it
  settles. This is why `.coverage` and `.claude/scheduled_tasks.lock` were NOT
  added in 2026-09 despite being the file types the old deletion blindness
  stranded: they appear in 4 boxes out of 120, and 2,980 invalidated baselines
  is the wrong price for that. **If the list must change, batch every wanted
  line into ONE edit so the fleet pays once**, and expect the check to be red
  until the passes finish.
## Backups and recovery history

- **Sync backups**: every sync writes the files it is about to overwrite or
  delete into `sync_backups/<sync ULID>/` (local `~/.boxyard/sync_backups/` for a
  pull, `<store>/sync_backups/` on the remote for a push) and purges that
  directory when it finishes. The directories are keyed by ULID, never by index
  name, and **nothing in boxyard ever reads one back** — they exist so a human
  can recover by hand. A purge whose failure was silently discarded leaked 1,186
  directories / 116.4 GiB onto one remote between 2025-11 and 2026-08;
  `rclone_purge` now raises, and `doctor`'s `orphaned-sync-backups` /
  `orphaned-remote-sync-backups` count whatever is left behind.
  **`discard-local` keeps the work it discarded in this same directory**
  (`delete_backup=False`), ULID-named and claimed by no sync record, so doctor
  cannot tell a deliberate keepsake from failed-purge residue and its hint says
  so. Never make that check auto-delete, and never assume a finding there is
  disposable: "no record names this ULID" proves no sync is waiting, not that
  the contents exist anywhere else.


## Related designs

- [Full-pass skip design and review history](../_dev/FULL-PASS-SKIP-DESIGN-NOTE.md)
- [Restic DATA design](../_dev/RESTIC-DATA-STORAGE-DESIGN-NOTE.md)
- [Checkout-root guide](checkout-roots.md)

# Findings — 00_rclone_perms_baseline

**Question:** does rclone (as boxyard invokes it) preserve the exec bit, and is
`--metadata` a viable cheap fix for the SFTP backend? Run `bash run.sh`
(self-contained; stands up a local `rclone serve sftp`, never touches hetzner).

## Results (rclone v1.74.4)

| Case | flags | outcome |
|------|-------|---------|
| local → local | (none) | exec bit **lost** (`src=x, dst=-`) |
| local → local | `--metadata` | exec bit **preserved** (`src=x, dst=x`) |
| local → SFTP → local | (none) | exec bit **lost** |
| local → SFTP → local | `--metadata` | exec bit **still lost** — on-SFTP mode is `664`, x gone on arrival |
| pure `chmod +x`, no content change → SFTP | `--metadata` | **not propagated** — rclone `Checks: 1/1, Transferred 0 B`, dst stays non-exec |

## What this means for production design

- **Confirmed decisive:** `--metadata`/`-M` is a **no-op for permissions over
  SFTP** — the exec bit is gone the instant the file lands on the SFTP backend
  (mode became `664`), regardless of `--metadata`. Since `hetzner-box`
  (`type = sftp`) is the only real backend, Option A from the research is dead for
  the actual use case. This matches rclone's feature matrix (SFTP metadata = `-`)
  and the open, unimplemented [rclone#7310].
- **Confirmed the caveat:** even where metadata *is* supported, a content-free
  `chmod` is never propagated (rclone only re-syncs metadata when the object is
  re-uploaded). So `--metadata` couldn't serve the exact "I just `chmod +x`'d a
  script" case even on a local/S3 backend.
- `--metadata` **does** work local→local — worth keeping as an optional extra for
  future local/S3 stores, but never as the primary mechanism.
- → The sidecar manifest (B1) is necessary, not just preferable.

## Surprises / notes

- `rclone serve sftp` applied its own umask, landing files at `664` not `644` —
  irrelevant to the exec-bit question but a reminder that the destination
  filesystem's umask, not the source mode, governs SFTP writes.

## Artifacts

- [`run.sh`](run.sh) — the truth-table harness.

[rclone#7310]: https://github.com/rclone/rclone/issues/7310

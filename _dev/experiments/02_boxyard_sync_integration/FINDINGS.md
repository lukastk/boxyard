# Findings — 02_boxyard_sync_integration

**Question:** does the wired-in manifest work end-to-end through the real
`boxyard` CLI against the actual hetzner-box (SFTP) remote? Run `bash e2e.sh`
(creates a throwaway `zzz-perms-e2e` box, pushes, copies back, asserts, deletes).
**Result: PASS.**

## What was proven

- `boxyard new -c` + `boxyard sync -d push` generates `.boxyard-perms.json` and
  ships it over SFTP. Confirmed by reading it back off the remote:
  `rclone cat hetzner-box:boxyard/boxes/<idx>/data/.boxyard-perms.json` returns
  `{"version":1,"executable":["... run.sh"]}`.
- `boxyard copy` (copy-from-remote) into a clean dir came back with
  `run.sh` as `-rwxrwxr-x` (executable) and `notes.txt` as `-rw-rw-r--`
  (non-exec). Since the transport strips the bit, the only thing that restores it
  is `apply_exec_manifest` finding the shipped manifest — so this is end-to-end
  proof over the production backend.
- The `664 -> 775` result confirms the "mirror read bits into exec bits" apply
  rule works on real files.

## Surprises / notes

- **`boxyard new` git-inits the box**, so a fresh box contains `.git/`, and the
  manifest captured all executable `.git/hooks/*.sample` files alongside `run.sh`.
  This is harmless and correct (they *are* executable), but it means manifests
  for real boxes will include git-internal executables. The walk cost is on par
  with boxyard's existing full-tree `check_last_time_modified` scan, so no new
  perf concern. Left comprehensive for simplicity; excluding `.git` from the walk
  is a trivial future tweak if the noise ever matters.
- The first `e2e.sh` run printed a scary "manifest not found on remote" at step 5
  — a **bug in the test script's index_name grep** (it assumed a 6-digit time
  component, but boxes can use the date-only id format `20260710_<subid>`), not a
  product issue. The re-run with the id taken from `boxyard path` confirmed the
  manifest is present.

## Artifacts

- [`e2e.sh`](e2e.sh) — the real-remote round-trip harness (self-cleaning).

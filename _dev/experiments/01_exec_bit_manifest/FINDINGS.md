# Findings — 01_exec_bit_manifest

**Question:** does the B1 exec-bit sidecar manifest actually work end-to-end over
a real SFTP round-trip — including the pure-`chmod` case `--metadata` can't do?
Run `bash test.sh` (self-contained local SFTP). **Result: 11/11 checks pass.**

## What was validated

- **Baseline (T1):** without the manifest, `+x` is lost on round-trip (as 00 showed).
- **Restore (T2):** generate-before-push + apply-after-pull restores `+x` on
  `script.sh` and `sub/tool`, leaves `data.txt` non-exec, and the symlink
  survives as a symlink (rclone `--links` recreates it; the manifest correctly
  skips it).
- **THE CRUX (T3):** `chmod +x data.txt` with **no content change**, regenerate,
  push, pull, apply → the bit propagates to the other machine, and the file's
  bytes are byte-identical (md5 match). Verbose rclone shows only
  `.boxyard-perms.json: Copied (replaced existing)` — `data.txt` was **not**
  re-transferred. This is exactly the case `--metadata` fundamentally cannot
  handle, and the manifest handles it for free because flipping the bit changes
  the *manifest's* content.
- **Clearing (T4):** `chmod -x` propagates too — apply clears the bit, so it's
  symmetric, not just additive.
- **Idempotency (T5):** a second `apply` is a no-op (returns no changes).

## Design decisions this locks in

- **Manifest format:** sorted JSON `{relpath: bool}` (owner-x bit), one file at
  the box-data root, name `.boxyard-perms.json`. Plain, diffable, tiny.
- **Apply semantics:** `exec → mirror read bits into exec bits` (`644→755`,
  `664→775`); `non-exec → clear all exec bits`. Deterministic, no umask
  surprises. (Chosen over `cur | 0o111`, which would grant x to group/other even
  where they can't read.)
- **Skips:** symlinks (shipped via `--links`) and the manifest file itself.
- **Push/pull asymmetry is correct:** generate must run *before* push (so it's
  included); apply must run *after* pull, over **all** manifest entries (rclone
  won't tell us which files it touched — and idempotent apply makes that safe).

## Deferred / still to check at integration time (→ experiment 02)

- Interaction with boxyard's include/exclude/**filters** and
  `DEFAULT_RCLONE_EXCLUDE`: confirm nothing drops `.boxyard-perms.json`
  (default exclude only lists `.venv/`, `__pycache__/`, `.DS_Store`, etc., so it
  should be safe — but a user `.rclone_exclude` could bite).
- Exact hook wiring around the single `DATA` `sync_helper` call in
  `03_sync_box.pct.py`, gated on `sync_direction` and the `preserve_permissions`
  toggle.
- Directory exec bits are intentionally **not** tracked (only regular files).

## Artifacts

- [`perms_manifest.py`](perms_manifest.py) — `generate()` / `apply()` prototype.
- [`test.sh`](test.sh) — the SFTP round-trip test harness.

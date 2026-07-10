# Experiments plan — preserving `+x` in boxyard (approach B1)

Throwaway prototyping for the **exec-bit sidecar manifest** (approach B1 from
[`../research/preserving-permissions.md`](../research/preserving-permissions.md)).
Each experiment is a numbered subdirectory under `_dev/experiments/`. The only
deliverable is **learnings** — written in the experiment's `FINDINGS.md`, with a
short summary + link under the experiment's "Findings" section here. Experiment
code is throwaway; production code gets rewritten once we're confident.

Status legend: `todo` · `in progress` · `done` · `skipped`.

---

## Group A — rclone behaviour (derisk the assumptions the research made)

### `00_rclone_perms_baseline`

**Status:** done (2026-07-10)

**Questions**
- Confirm empirically that rclone (as boxyard invokes it, `--links --fast-list`,
  no `--metadata`) drops the exec bit: (a) local→local, (b) local→SFTP→local.
- Confirm `--metadata`/`-M` **preserves** exec bit local→local (where supported).
- Confirm `--metadata` is a **no-op for mode over SFTP** (the load-bearing claim
  that kills the cheap Option A for the real hetzner-box backend).
- Confirm the "metadata only re-syncs when the object is re-uploaded" caveat: a
  pure `chmod +x` (no content change) is NOT propagated by `--metadata`.

**Deliverable**
- A self-contained script standing up a local `rclone serve sftp` remote + rclone
  config, running the matrix of transfers, and printing a truth table of
  exec-bit outcomes. No touching the real hetzner-box.

**Findings** *(full writeup in [`00_rclone_perms_baseline/FINDINGS.md`](00_rclone_perms_baseline/FINDINGS.md))*
- Empirically confirmed (rclone v1.74.4): exec bit is lost on local→local and
  local→SFTP→local without `--metadata`.
- `--metadata` preserves it **local→local** but is a **no-op over SFTP** (file
  lands `664`, x gone) — Option A is dead for the real hetzner-box backend.
- Pure `chmod +x` (no content change) is **never propagated** by `--metadata`
  (rclone `Transferred 0 B`). → B1 sidecar is necessary.

---

## Group B — the B1 manifest itself

### `01_exec_bit_manifest`

**Status:** done (2026-07-10)

**Questions**
- Manifest mechanics: walk the tree, capture per-file exec bit, serialise,
  re-apply on the other side, idempotency.
- **The correctness crux:** a pure `chmod +x` with no content change — does the
  manifest file's content change (so rclone re-syncs it) and does apply-on-pull
  restore the bit even though rclone never re-transfers the target file?
- `chmod -x` (clearing the bit) propagates too (not just additive).
- What exact chmod semantics to use on apply (umask interaction; do we mirror
  read bits, force `a+x`, or store the literal mode?).
- Edge cases: the manifest must exclude itself; symlinks (boxyard serialises them
  to `.rclonelink` under `--links` — do they need handling?); directories;
  files present in manifest but deleted; new files absent from manifest.

**Deliverable**
- Prototype `perms_manifest.py` with `generate()` / `apply()` + a test script
  that drives a full push→wipe-modes→pull cycle through the SFTP remote from 00
  and asserts exec bits survive, including the pure-`chmod` case.

**Findings** *(full writeup in [`01_exec_bit_manifest/FINDINGS.md`](01_exec_bit_manifest/FINDINGS.md))*
- **11/11 checks pass** driving the manifest through a real SFTP round-trip.
- Restore, clear (`chmod -x`), idempotency, and symlink-survival all work.
- **The crux works:** pure `chmod +x` (no content change) propagates — only the
  manifest re-syncs, the target file is byte-identical (md5 match), yet the bit
  is restored on the far side. This is what `--metadata` can't do.
- Locks in: sorted-JSON `{relpath: bool}` at data-root named
  `.boxyard-perms.json`; apply mirrors read→exec bits, clears when non-exec;
  skips symlinks + the manifest itself; generate-before-push / apply-after-pull.

---

## Group C — integration shape

### `02_boxyard_sync_integration`

**Status:** done (2026-07-10)

**Questions**
- Wire generate-before-push / apply-after-pull around a *real* `boxyard sync` of
  a box (temp `BOXYARD_CONFIG_PATH`, `fake` local store or a local SFTP), with an
  executable script in the box, and confirm the hook point in `sync_box`
  (the `DATA` part) behaves and doesn't fight boxyard's filters/`.rclonelink`.
- Where should the manifest live — `data/` root vs `conf/`? Does it survive the
  include/exclude/filters machinery?

**Deliverable**
- A scratch harness that creates a box, runs the manifest logic around a real
  sync between two local box roots, and verifies `+x`.

**Findings** *(full writeup in [`02_boxyard_sync_integration/FINDINGS.md`](02_boxyard_sync_integration/FINDINGS.md))*
- **PASS** end-to-end through the real `boxyard` CLI against hetzner-box (SFTP):
  push ships `.boxyard-perms.json` (confirmed by reading it back off the remote),
  and `boxyard copy` restores `run.sh` to `-rwxrwxr-x` while `notes.txt` stays
  non-exec.
- `boxyard new` git-inits boxes, so manifests include `.git/hooks/*.sample`
  (harmless/correct). Manifest lives at the `data/` root and rides the normal
  filters fine.
- Wired into `sync_helper` (push=generate / pull=apply, gated on
  `preserve_exec_perms`), with `sync_box` (DATA), `force_push`, and
  `copy_from_remote` as the call sites.

---

## Resolved decisions (2026-07-10)

- **Approach chosen:** B1 — exec-bit-only sidecar manifest (git-style), not full
  mode. Rationale in the research doc: matches the "+x" priority and avoids
  cross-machine uid/gid/umask breakage.
- **Always-on:** no user toggle; `sync_box` always preserves exec bits for DATA.
- **v1 apply is additive-only:** restores `+x`, never clears it — a stale
  manifest can't *destroy* a bit, so mixed old/new boxyard versions stay safe
  during rollout. Propagating `chmod -x` deferred to v2 once all machines run new.
- **Manifest:** `.boxyard-perms.json` at the DATA root; sorted-JSON
  `{"version":1,"executable":[relpaths]}`.
- **Apply semantics:** mirror read bits into exec bits (`644->755`, `664->775`);
  skip symlinks + the manifest itself.
- **Tooling gotcha:** exports MUST use nblite **>= 1.2.2**; 1.2.1 emits broken
  relative imports for function-export modules.

## Still to decide

- Whether to add a `boxyard doctor` permissions-drift check (deferred; nice-to-have).
- v2: propagate `chmod -x` (clearing) once every machine runs the new code.

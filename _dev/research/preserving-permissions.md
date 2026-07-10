# Research: preserving access permissions (especially `+x`) in boxyard

Ticket: *Research ways to preserve access permissions (especially `+x`) in boxyard*
(`8480a20d-f4fc-46bc-9bc5-aacd687db5cf`). Date: 2026-07-10.

> **Status: IMPLEMENTED (v0.3.0, 2026-07-10).** Option B1 shipped — an exec-bit
> sidecar manifest (`.boxyard-perms.json`), always-on, additive-only in v1. See
> `boxyard._utils/perms.py`, the derisking in `../experiments/`, and CHANGELOG
> 0.3.0. This document is the original research; it is kept for rationale.

## TL;DR

- **Today boxyard loses all Unix modes on sync.** Every rclone command it issues
  (`sync`, `bisync`, `copy`, `copyto`) is built by `_rclone_cmd_helper` in
  `pts/mod/_utils/01_rclone.pct.py`, and none of them pass `--metadata`. Files
  land on the other side with the destination filesystem's default umask
  permissions, so the executable bit is dropped.
- **The obvious minimal fix — add `--metadata` / `-M` — does not work for your
  actual setup.** Your only real storage backend is `hetzner-box`, `type = sftp`
  (from `~/.config/boxyard/boxyard_rclone.conf`). **rclone's SFTP backend has no
  metadata support** (it shows `-` in rclone's feature matrix; the request to add
  `mode/uid/gid/atime/mtime` is [rclone#7310](https://github.com/rclone/rclone/issues/7310),
  still **open / help-wanted** as of 2026-04-21, no PR). So rclone has nowhere to
  put the mode when the file passes through SFTP, and `--metadata` is a no-op for
  permissions there.
- **`--metadata` also can't propagate a pure `chmod +x`.** Per rclone's docs,
  *"metadata will be synced from the source object to the destination object only
  when the source object has changed and needs to be re-uploaded."* A `chmod +x
  foo.sh` that doesn't change file contents would never propagate — a fatal gap
  for the exact use case in the ticket.
- **Recommended answer (moderate effort, no rewrite):** a **permissions sidecar
  manifest stored inside the box's `data/` part**. boxyard records each file's
  mode (or just its exec bit) in a small file that syncs like any other content
  over any backend, and re-applies it on pull. This is backend-agnostic (works
  over SFTP), survives content-free `chmod`s, and is conceptually identical to
  how rclone already serialises symlinks to `.rclonelink` files under `--links`,
  and to how git tracks only the exec bit.
- Drastic alternatives (rsync/restic transport, tar-per-box, git-backed data,
  upstreaming SFTP metadata to rclone) are catalogued at the end.

---

## 1. Why permissions are lost today

`sync_box` (`pts/mod/cmds/03_sync_box.pct.py`) syncs each box **part**
(`DATA`, `META`, `CONF`) through `sync_helper`
(`pts/mod/_utils/02_sync_helper.pct.py`), which calls `rclone_sync`. The command
is assembled here:

```python
# pts/mod/_utils/01_rclone.pct.py  — _rclone_cmd_helper
cmd = [
    get_rclone_binary(), cmd_name, "--config", rclone_config_path,
    "--links", source_spec, dest_spec,
]
if use_fast_list: cmd.append("--fast-list")
# ... include/exclude/filter/progress ...
```

There is no `--metadata`. rclone therefore creates destination files with the
destination's umask; the source mode (including `+x`) is discarded. This affects
the user-facing box content: locally the `DATA` part is `~/dev/<index_name>`
(`BoxMeta.get_local_part_path(..., BoxPart.DATA)`), i.e. the folder you actually
`cd` into and run scripts from.

Note the existing precedent: boxyard already passes `--links`, which makes rclone
represent a filesystem feature it can't store natively (symlinks) as sidecar
`.rclonelink` text files that travel with the data. The recommended option below
is the same idea applied to file modes.

---

## 2. The hard constraint: your backend can't carry metadata

| Backend        | rclone metadata support | Relevant to you |
|----------------|-------------------------|-----------------|
| **sftp** (`hetzner-box`) | **none** (`-` in matrix; [#7310](https://github.com/rclone/rclone/issues/7310) open) | **This is your real remote** |
| local (`fake`) | full (`DRWU` — read/write system + user metadata) | test/fake store only |
| s3, etc.       | full (mode/uid/gid stored as object metadata) | not configured |

So any approach that relies on rclone to *store* the mode on the remote is
blocked by the SFTP backend regardless of rclone version (you're on v1.74.4,
which is current). The mode has to be carried as ordinary file **content**, or a
different transport has to be used.

---

## 3. Minimal options (no architectural change)

### Option A — add `--metadata` to rclone commands  ⚠️ insufficient for you

Add `cmd.append("--metadata")` in `_rclone_cmd_helper` (one line, plus an opt-in
flag). What it buys:

- **local ↔ local** (the `fake` store, and any future local backend): preserves
  mode/uid/gid/atime/mtime. Works.
- **local ↔ SFTP (your case): no effect on permissions** — SFTP backend has no
  metadata support.
- Even where it works, it **won't propagate a `chmod` with no content change**
  (rclone only re-syncs metadata when the object is re-uploaded).

**Verdict:** cheap and harmless to add as a *partial* measure (helps if you ever
add an S3/local-backed store), but it does **not** solve the ticket for the
SFTP-backed reality, and it misses the pure-`chmod` case. Do not treat it as the
solution.

### Option B — permissions sidecar manifest inside the box  ✅ recommended

Store the modes as ordinary content that syncs over *any* backend, and re-apply
them on pull. Concretely:

- Keep a manifest, e.g. `~/dev/<index_name>/.boxyard-perms.json` (lives at the
  root of the `DATA` part, so it syncs with the data — dotfiles are not excluded
  by `DEFAULT_RCLONE_EXCLUDE`). Format: a map of relative path → mode.
- **On push (DATA):** before the rclone push, walk the local `DATA` tree and
  (re)generate the manifest from current modes.
- **On pull (DATA):** after the rclone pull, read the manifest and `chmod` each
  listed file to its recorded mode (idempotent; apply to all, since rclone won't
  tell us which files it touched).

Natural hook point: the single `DATA` `sync_helper` call in `sync_box`
(`03_sync_box.pct.py:~329`), gated on `sync_direction` — generate-before-push,
apply-after-pull. `sync_helper` already receives `sync_direction`, so this is a
localized change, not a rewrite. Could be gated by a `preserve_permissions`
toggle in `boxmeta.toml` or global config (mirrors the existing per-box
`.rclone_include/.rclone_exclude/.rclone_filters` mechanism in the `CONF` part).

Two granularities:

- **B1 — exec-bit only (recommended).** Record only whether each file is
  executable; on pull, set/clear `+x` (respecting umask for the rwx groups),
  exactly like **git**, which tracks `100644` vs `100755` and nothing else. This
  matches the ticket's stated priority ("especially `+x`"), and sidesteps the
  cross-machine headaches of full modes (your uid/gid and umask differ across
  macbook/macstudio/mymain/ideapad; blindly restoring `0640`/owner across
  machines is often wrong).
- **B2 — full mode.** Record the full `st_mode` permission bits. More faithful,
  but you must decide policy for uid/gid (almost certainly *don't* try to restore
  owner across machines) and for umask interaction.

Why this is the right shape:

- **Backend-agnostic** — works over SFTP today, and over anything else later.
- **Handles pure `chmod`** — flipping `+x` changes the manifest's *content*, so
  the manifest file itself is re-synced and the bit is restored on pull, even
  though the target file's bytes never changed. (This is the case `--metadata`
  fundamentally cannot handle.)
- **Discoverable & debuggable** — the manifest is plain text you can read, diff,
  and reason about. No hidden state.
- **Cheap** — a stat-walk on push and a chmod-walk on pull, both O(files).

Caveats to design around:

- The manifest is one more visible dotfile in the box root (acceptable — like
  `.git`). Alternatively store it under the `CONF` part instead of `DATA` so it's
  out of the working tree; then it must be generated from `DATA` but synced with
  `CONF`. `DATA`-root is simpler and keeps push/pull symmetric.
- Must exclude the manifest from *being governed by itself* (skip it in the walk).
- Directory modes and new-file races: regenerate wholesale each push rather than
  trying to diff.
- Conflict semantics stay trivial because boxyard's sync is record-gated and
  one-directional per run (not content-merged), so the manifest never has to be
  three-way-merged.

### Option C — heuristic post-sync `chmod`  ✗ not recommended

Detect "probably executable" files after pull (shebang line, `.sh`/`.py` with
`__main__`, files already `+x` on the other machine's history) and `chmod +x`
them. This is guessy, silently wrong at the edges, and exactly the kind of
fallback-hack the project's guidelines warn against. Listed only for
completeness.

---

## 4. Drastic alternatives (larger, more principled)

### D — pluggable sync engine; use `rsync` for SSH-reachable remotes

`hetzner-box` is reached over SSH/SFTP, so plain `rsync -a` (or `rsync -pt`)
would preserve modes (including `+x`) natively, incrementally, without any
sidecar. This means abstracting the transport in `sync_helper` behind an
"engine" interface (rclone | rsync) chosen per storage backend. Upside: correct
permissions *and* the full rclone machinery stays for non-SSH backends. Downside:
boxyard's safety model (sync records, `--backup-dir`, interrupted-sync recovery,
`lsjson`-based status) is all rclone-shaped; a second engine has to re-implement
or bypass that. Substantial but architecturally clean if permissions become
first-class.

### E — snapshot transport (restic / borg) for the `DATA` part

restic/borg preserve full metadata (mode, owner, times, xattrs), dedupe, and give
versioned snapshots for free — a strict superset of what `--backup-dir` gives you
today. Would replace rclone for `DATA` transport while keeping boxyard's box
model. Big change: new dependency, new on-remote format, status/conflict logic
rewritten around snapshots.

### F — archive-per-box (tar) over rclone

Pack each box's `DATA` into a `tar` (preserves all modes) and let rclone ship the
tarball; unpack on pull. Minimal new dependencies. But it **destroys rclone's
incremental sync** (whole-box re-transfer on any change), breaks
`--backup-dir`/bisync/interrupted-recovery, and kills remote browsability of box
contents. Poor fit for boxes that are large or frequently touched. Not
recommended.

### G — git-backed `DATA`

Store each box's data in git. git natively preserves the exec bit and gives full
history/merge. But it changes boxyard's entire storage/identity model, adds
git-scaling problems for big/binary boxes, and duplicates the sync-conflict logic
that already exists. Large; only worth it if you want versioning too.

### H — upstream: add SFTP metadata support to rclone ([#7310](https://github.com/rclone/rclone/issues/7310))

Since your remote is a genuine Unix filesystem over SSH, setting `mode` on write
and reading it back is straightforward at the protocol level — the feature is
"help wanted," unassigned, with no PR. Implementing it upstream would make
`--metadata` (Option A) "just work" for SFTP, benefiting everyone. Highest
leverage in principle, but an external dependency on an uncertain timeline and it
*still* wouldn't fix the pure-`chmod`-not-re-uploaded gap.

---

## 5. Recommendation

1. **Do Option B1 (exec-bit sidecar manifest, git-style), gated behind a
   `preserve_permissions` toggle.** It's the only minimal-footprint option that
   actually works over your SFTP backend, handles bare `chmod +x`, and stays
   discoverable. Hook it into the `DATA` sync in `sync_box`
   (`03_sync_box.pct.py`), generate-before-push / apply-after-pull.
2. Optionally also add `--metadata` (Option A) behind the same toggle **for
   local/S3 backends only**, as a belt-and-suspenders for future non-SFTP stores
   — but don't rely on it for SFTP.
3. Add a `boxyard doctor` check for "permissions drift" (files whose current mode
   disagrees with the manifest) so loss is *visible* rather than silent.
4. Treat D (rsync/restic engine) as the real long-term answer only if you decide
   full metadata fidelity (owner, times, xattrs) — not just `+x` — becomes a
   first-class boxyard guarantee.

## Sources

- rclone `--metadata` docs (system metadata keys; "only when re-uploaded"):
  <https://rclone.org/docs/#metadata>
- rclone feature/metadata matrix (SFTP = `-`, Local = `DRWU`):
  <https://rclone.org/overview/>
- rclone SFTP backend docs (no metadata table): <https://rclone.org/sftp/>
- rclone#7310 — Support `mode/uid/gid/atime/mtime` metadata for SFTP (open,
  help-wanted): <https://github.com/rclone/rclone/issues/7310>
- Codebase: `pts/mod/_utils/01_rclone.pct.py`, `pts/mod/_utils/02_sync_helper.pct.py`,
  `pts/mod/cmds/03_sync_box.pct.py`, `pts/mod/_models.pct.py`, `pts/mod/const.pct.py`.

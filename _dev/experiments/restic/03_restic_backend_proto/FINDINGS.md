# 03 — restic DATA backend prototype: findings

**Status:** done, 2026-08-30. 30/30 checks pass.

```bash
uv run python _dev/experiments/restic/03_restic_backend_proto/e2e.py
```

Stands up a throwaway two-machine boxyard under `/tmp` (via
`tests.integration.conftest.create_boxyards`), drives the prototype backend
through it, and tears it down. Touches nothing live.

---

## 1. restic reaches the real remote through boxyard's own rclone config

```
restic -o rclone.program="rclone --config ~/.config/boxyard/boxyard_rclone.conf" \
       -r rclone:hetzner-box:<path>
```

Confirmed end to end against `hetzner-box:boxyard-restic-probe/` — a deliberately
scoped write, purged afterwards with the deletion verified.

This is the property that makes restic acceptable at all: it inherits boxyard's
storage-location abstraction rather than replacing it with restic's own much
narrower backend list. It is worth more than the raw speed.

## 2. Against plain `rclone sync`, on the same tree

`20260601_zbl55q__sesh--major-review`, 4,855 files / 49.6 MiB. This is a p97 box
by file count — the yard median is 196 inodes — so it is not a pathological case.

| | restic | plain rclone sync | ratio |
|---|---|---|---|
| first push | 4.6 s | 402.5 s | 87x |
| no-op push | 3.1 s | 30.7 s | 10x |
| cold pull | 3.4 s | 227.1 s | 67x |
| no-op pull | 2.7 s | 30.8 s | 11x |
| remote objects | 10 | 4,855 | 486x |
| remote bytes | 19.1 MiB | 49.6 MiB | 2.6x |
| `restic init` | 4.1 s | — | |

Raw SFTP throughput on this link measured separately at **11.8 MiB/s**, so the
rclone figures are round-trip-bound, not bandwidth-bound. That is the whole
story: a round trip costs 0.67 s whatever the file weighs.

**This contradicts the ticket's "push is excellent and pull is not."** At this
scale the no-op restore is *faster* than cold (2.7 s vs 3.4 s). The honest
statement is `push = O(changed bytes)`, `pull = O(destination tree)` — pull does
not get the asymptotic win, but it stops paying per-file remote round trips,
which is what actually costs hours today.

## 3. The snapshot-ID skip filter is real, and keyless

A repo's `snapshots/` filenames **are** the snapshot IDs. Took a new snapshot;
the plain directory listing gained exactly one file whose name equals the new
snapshot's full ID. One `rclone lsjson` of that directory costs 0.6 s and needs
no key.

**Corollary that shapes the design:** this only works keylessly for **per-box**
repos. A shared repo has one `snapshots/` directory holding 590+ files with
nothing outside the encryption saying which box each belongs to.

**Better still, don't spend a per-box listing on it.** Write the snapshot ID to
`boxes/<box>/data.snapshot`, at depth 2 beside `boxmeta.toml`. The bulk listing
`sync-missing-meta` and `--skip-unchanged-meta` *already run* then answers "did
this box's DATA move" for the whole yard in the same single call — zero
additional remote calls per pass.

## 4. Targeted restore beats full restore, and is correct

At jackfruit scale (134,551 files / 3.760 GiB, v1→v2 differing by 50 edited
source files), local repo:

| | time |
|---|---|
| cold full restore | 24.1 s |
| full restore over an identical tree | 14.3–55.8 s |
| full restore with 50 stale files | 16.3–64.8 s |
| `restic diff` | 2.3 s |
| `restore --include` (50 paths) | 0.9 s |
| **diff + targeted restore (3 reps)** | **3.1 / 3.3 / 3.1 s** |

`diff -r` between the targeted result and a full restore is clean — byte
identical. The full-restore spread is wide because the machine was at load 6.6
from the live supervisor; the targeted figure is stable because it barely touches
the disk.

## 5. Two traps that would otherwise have shipped

### `restore --include` does not delete

Verified: a stray file absent from the snapshot survives an `--include` restore,
and is only removed by a full `restore --delete`. So the pull must apply the
diff's `-` lines as explicit local deletions. Without it a file deleted on
another machine silently comes back — the exact class of bug tombstones exist to
prevent. The e2e driver tests this directly.

### restic records the pusher's absolute path, and `restic diff` compares by it

Two snapshots of **byte-identical** content backed up from different paths:

```
Files:       2 new,   2 removed,  0 changed
Data Blobs:  0 new,   0 removed        <- dedup is perfect; the DIFF is useless
```

Live risk here: mymain already has two checkout roots (`~/dev`,
`~/hetzner_volume/boxes`) and the Macs use `/Users/lukastk`. A naive diff-driven
pull would delete the tree and restore it.

Four normalisation routes tested, all dead:

| route | outcome |
|---|---|
| `restic backup --set-path` | does not exist |
| `restic rewrite` | changes host and time, **not** paths |
| `cd <box> && restic backup .` | still records the absolute path |
| backup a symlink at a fixed canonical path | records the fixed path but archives **the symlink** (0 B snapshot) |

A bind mount would work and needs root. Rejected.

**The fix:** `restic restore <snap>:<source_path> --target <dir>` places the
snapshot's *contents* at the target, so the local path need not match the
pusher's. With that form `--include` anchors to the subpath by a leading slash:

| pattern | result |
|---|---|
| `--include /coordination/FLOOR-REPORT.md` | exactly that file — correct |
| `--include coordination/FLOOR-REPORT.md` | works, unanchored |
| `--include package.json` | 98 unrelated matches — trap |
| `--include /abs/path/…` | 0 files — trap |

Note `restic diff` does **not** accept the `<snap>:<subpath>` form (it reports
`path tmp: not found`), so the fallback for mismatched source paths is a full
subpath restore, not a subpath diff.

### And one the prototype caught in itself

`backup --dry-run` reports **`Dirs: 0 new, 1 changed`** on a perfect no-op — the
root directory's own metadata is re-read. Consulting `dirs_changed` as "this box
is modified" makes every box permanently `NEEDS_PUSH` and silently disables the
whole skip filter. Use `files_new`, `files_changed`, `dirs_new` only. This is why
check 4 failed on the first run.

## 6. restic carries Unix mode natively — the exec-bit manifest is redundant

Through the exact restore form this design uses (`restore <snap>:<source> --target`),
with `umask 022`:

| file | source | restored |
|---|---|---|
| `run.sh` | 755 | **755** |
| `notes.txt` | 644 | **644** |
| `sub/tool.sh` | 775 | **775** |
| `link.txt` | symlink | symlink |

Exact, including 775 — which `.boxyard-perms.json` cannot express, since it
stores a boolean per path and reconstructs the mode by mirroring read bits into
exec bits, and its v1 is additive-only (restores `+x`, never clears it).

So `preserve_exec_perms` is skipped for restic-backed DATA, `_utils/perms.py`
stays for plain boxes, and the push excludes the manifest so a converted box does
not carry one into every snapshot.

**Trap — `backup --exclude` and `restore --include` anchor by OPPOSITE rules.**
Measured with a manifest at the DATA root and another in a subdirectory:

| `backup --exclude …` | manifests left in the snapshot |
|---|---|
| `.boxyard-perms.json` | **0** — unanchored basename, excludes both |
| `/.boxyard-perms.json` | **2** — anchors at the filesystem root, excludes neither |
| `<abs data path>/.boxyard-perms.json` | **1** — excludes exactly the root one ✔ |

`backup` patterns match the **absolute** path; `restore --include` under the
`<snap>:<subpath>` form anchors with a **leading slash relative to the subpath**.
Same-looking flags, opposite rules, and both failure modes are silent.

## 7. Concurrent writers are fine; only `prune` is not

Two `restic backup` runs into one repo with separate cache directories (two
machines simulated): both exit 0, both snapshots recorded, no leftover locks.
`backup` takes a non-exclusive lock.

So `write_owner` does **not** need to become mandatory for restic-backed boxes —
restic makes unowned boxes safer, not less safe.

**But `forget` and `prune` both DO take exclusive locks**, and that was measured
rather than assumed after an earlier draft claimed `forget` was cheap enough to
run on every push. `forget --keep-last 3` is indeed cheap in work — 0.78 s,
removed 9 snapshot files, left all 22 packs — but run concurrently with a
`backup` against the same repo, **3 trials of 3** ended with one side exiting
**11, failed to lock repository** (the backup lost once, the `forget` twice).
`prune` for contrast: 0.80 s, repacked 24 blobs, removed 17 of 22 packs.

On a five-machine fleet a `forget` inside the sync loop would intermittently fail
other machines' pushes. Both belong in an explicit maintenance command, never in
the loop.

## 8. Conversion is safe — but only with step 4

Tested with an un-upgraded machine present, holding a real local checkout.

| what was done | what the un-upgraded machine does |
|---|---|
| remove remote `data/` only; non-owner, no local changes | reports `SYNCED`, does nothing. Safe but **silent** |
| remove remote `data/` only; **unowned** box, local changes | **resurrects the plain `data/` on the remote** — box now exists in both formats and diverges unreported |
| remove remote `data/` **and** `sync_records/<box>/data.rec` | **refuses loudly**; nothing resurrected, local data intact |

So the procedure is, and the order matters:

1. push the repo to `boxes/<box>/data.restic/`
2. verify a restore is byte-identical **before destroying anything**
3. purge `boxes/<box>/data/`
4. delete `boxyard/sync_records/<box>/data.rec` ← **without this, row 2**
5. write `boxes/<box>/data.snapshot`; set `storage_format` in `boxmeta.toml`

The gate works because it drives `get_sync_status` into its existing `ERROR`
branch, which `sync_helper` raises on for anything but `--force`. No fleet-wide
version negotiation, no heartbeat, no flag day.

Row 2 is not hypothetical: **321 of 594 boxes are unowned today.**

**Cost of the gate:** an un-upgraded machine logs one error per converted box per
pass — the "cries wolf" pathology v0.4.x removed. Bounded by the migration
window, and new boxyard must render this state as its own `SyncCondition`
(alongside `WRITE_DENIED` and `LOCAL_STORAGE`), not as an error.

## 9. Retention, and the bug it exposed

`--keep-within` is relative to the **newest snapshot**, not to now — restic says
so itself (`Applying Policy: keep all snapshots within 90d of the newest`), and
it was verified on a repo whose newest snapshot was back-dated 200 days:
`forget --keep-within 90d` kept 2 of 6 rather than deleting all six. That is what
makes a single duration string a safe shape for the retention policy even for
dormant boxes.

**The bug this exposed.** With a snapshot that no longer exists, restic's two
relevant commands disagree:

| | behaviour |
|---|---|
| `restic snapshots --json <gone>` | **rc=0**, stdout `[]`, warning on stderr |
| `restic backup --parent <gone>` | **rc=1**, no summary |

The prototype's `local_is_modified` used `rc != 0 -> assume changed`, so a clean
replica whose base had been forgotten by another machine's retention pass
reported **CONFLICT**: no data lost, but the box stops syncing there until a
human intervenes. On a fleet where pocket4 is offline for days, that is a
recurring false alarm on the machine least able to notice it.

Fixed by recording `synced_at_unix` alongside the snapshot ID and falling back to
the mtime-versus-record-time test the plain backend already uses. Now tested in
both directions (e2e check 9):

| | verdict | action |
|---|---|---|
| base forgotten, replica unmodified | `NEEDS_PULL` | full restore (`full-base-forgotten`), converges byte-identically |
| base forgotten, replica edited | `CONFLICT` | nothing touched, local work intact |

The pull layer degrades independently too: `snapshot_source_path` returns `None`
for a forgotten base, which routes to the full restore. Two independent reasons
it cannot act on a bad diff.

## 10. What the prototype locks in

- repo at `boxes/<index_name>/data.restic/`; pointer at
  `boxes/<index_name>/data.snapshot`; local state at
  `~/.boxyard/restic_state/<index_name>/data.json`, written via temp-file rename
  and degrading in one direction (missing ⇒ "do the work"), exactly like
  `_sync_policy`'s check records
- password fetched **once per process** into `RESTIC_PASSWORD`, never
  `--password-command` (`secret get` costs 0.77–1.51 s per call)
- pointer written **after** the snapshot exists, never before
- deletions confined to the box's own DATA directory, with `..` escapes dropped —
  a path out of the repo is data, not an instruction
- restic invoked as argv, never a shell string

## Not covered here

Migration throughput on real boxes, `prune`/`forget` cost and stale-lock
recovery, retention policy, and key rotation across 590 repos. See experiments
04–06 in the plan.

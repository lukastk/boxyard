# Boxes with very many files — recommendation

Ticket `0f3056f7`. Companion to `SYNC-CADENCE-DESIGN-NOTE.md`, which left two
things to this thread: whether `compress` is the right answer at all, and what
to do about the 10-hour box that blocks the scheduling loop.

Status: **recommendation only, nothing shipped.** Measured on mymain,
2026-08-27/28, against `hetzner-box` (SFTP, RTT 25.8 ms).

## Cost model

Everything below rests on four measured constants.

| operation | transactions | @ tps 10 | @ tps 50 |
|---|---|---|---|
| list one remote directory | ~1 | 0.10 s | 0.02 s |
| upload one small file | ~8 | 0.81 s | 0.17 s |
| server-side move (a `--backup-dir` delete) | ~3 | 0.31 s | 0.06 s |
| `rclone delete` one object | ~1 | 0.11 s | 0.02 s |

Two consequences worth stating plainly, because both contradict premises this
ticket started from:

- **On SFTP a traversal costs one transaction per _directory_, not per file.**
  rclone's sftp backend has no `ListR`, so `--fast-list` — which
  `_rclone_cmd_helper` passes on every call — is inert here. The ticket's
  "floor of ~7 hours in transactions alone" assumed per-file and is out by ~13x.
- **A single stream reaches 33 MB/s** (200 MB in 5.9 s). Bandwidth is never the
  constraint. Round trips are.

## Answer 1 — `compress`: do not implement it

Not "not yet". The cold policy as designed cannot be compressed, and the
benefit it would buy does not exist.

**It is not implementable for the boxes it targets.** The policy selects on
group membership, which is fleet-wide; packing needs a local copy, which is
per-machine. Measured: of the 504 boxes in `archived`/`dormant`, **425 (84%)
have no local DATA copy on mymain at all.** Compressing them would mean pulling
several TB down, packing, pushing back and deleting — on a machine with 16 GB
of free disk. Compressing only the 79 that happen to be checked out here would
leave the same box stored packed on one machine and unpacked on another, which
is precisely the fleet-wide inconsistency that `type` must never have.

**It would save no sync time.** After the design note's skip filter and the
batched status probe, a cold box already costs nothing:

- 425 of them have no local DATA, so DATA resolves to `EXCLUDED` and never syncs.
- One that _is_ local and unchanged returns `SYNCED` from the local mtime
  watermark; `sync_helper` returns before any `rclone sync` runs, so the
  directory traversal a pack was meant to avoid never happens.

The traversal only ever runs on a box that is **changing** — and a changing box
is the worst case for packing, because any edit repacks and re-uploads
everything.

**It would not relieve storage pressure either.** The remote is 5.0 TiB total,
3.3 TiB used, **1.7 TiB free**. Not a problem to solve today.

**And it is the wrong shape for the box that motivated it.** jackfruit is 73%
dormant bulk and 27% hot core (below). Repack-on-any-change would re-upload
89,307 unchanged files every time a hundred files move in a worktree. A large
cold body with a small hot core is the worst case for an archive and the best
case for what rclone already does.

If cheap cold storage is ever wanted, the honest tool is content-addressed
chunking — restic/borg/kopia pointed at the same storage box — not a bespoke tar
mode inside boxyard.

**On shipping the field anyway:** a config key that silently does nothing is a
hidden surprise of exactly the kind AGENTS.md forbids — someone sets
`compress = true` and believes their box is packed. Either leave it out, or ship
it with `doctor` reporting it as unimplemented. Adding it later costs one staged
config rollout, the same as any other key.

## Answer 2 — jackfruit: fix it, no lane needed

### Size is not the problem; churn is

Post-exclude, by subtree:

| subtree | files | dirs | changed/24 h | cost per pass |
|---|---|---|---|---|
| `worktrees/` — 13 dormant checkouts | 89,307 | 3,404 | **0** | 3,404 listings, no transfers ever |
| `jackfruit/` — live checkout + 4 active worktrees | 23,210 | 1,423 | **21,586** | all of it |
| `jackfruit/.git` | 8,035 | 514 | 4,713 | real, worth keeping |
| everything else | 936 | 80 | 8 | nothing |

73% of the box is dormant and costs one directory listing each per pass — 3,404
transactions, 5.7 min at tps 10 and 68 s at tps 50 — and never transfers a byte.
**A box being enormous is close to free once pushed.** What costs hours is the
26,307 daily changes at ~8 transactions each: 4.8 h/day at tps 10, 58 min at 50.

I withdraw the `worktrees/` exclude I recommended earlier. There are two
worktree trees in this box and I named the wrong one: the top-level
`worktrees/` had **zero** changes in 24 h. Excluding it would have dropped 73%
of the box, lost the backup of 13 real checkouts, and saved nothing.

### The largest remaining item is `paraglide`, and it is on no list

Daily churn in the four active worktrees:

| directory | changed/24 h | share | status |
|---|---|---|---|
| `apps/web/src/lib/paraglide` — generated i18n | **14,088** | **49%** | on no list |
| `.svelte-kit` | 4,167 | 14% | landed in myrig `44c7706` |
| `build` | 2,356 | 8% | under survey (`e599078f`) |
| `node_modules` | 492 | 2% | already excluded |
| real source | 7,682 | 27% | irreducible |

`paraglide` is gitignored inside the worktree, regenerated by the build, and is
**half this box's daily sync cost on its own**. No name-based survey finds it: it
sits four levels inside a source tree at `apps/web/src/lib/paraglide`, not at a
repo root.

Suggestion for the exclude survey: **rank candidates by churn, not by file
count.** File count finds big cold directories, which cost least. On this box
the two rankings barely overlap — the biggest directory has zero churn and the
biggest churner is not in the top ten by size. Carry one hazard: **gitignored is
not disposable.** `.env`, local config and credentials are routinely gitignored
and are exactly what you want backed up. Use each repo's own `git check-ignore`
as evidence for a human decision, never as the rule.

### What that adds up to

| step | churn/day | pass cost at 6 h cadence |
|---|---|---|
| today | 33,584 | hours |
| `.svelte-kit` (landed) | 29,417 | |
| + `paraglide` | 15,329 | |
| + `build` (if the survey agrees) | 12,973 | |
| + tpslimit 10 → 50 | 12,973 | **~10 min** (8 min transfer, 100 s traversal) |

So the wrinkle resolves itself: **jackfruit stops being a 10-hour box and
becomes a 10-minute one, with no lane, no packing and no new machinery** —
one exclude line and one environment variable.

`RCLONE_TPSLIMIT=10` arrived in myrig `2544245` to fix local CPU overload,
together with `max_concurrent_rclone_ops` 50 → 2 and `nice`/`ionice`; the
concurrency cap is what addressed it, and the loop has since gained `taskset`
pinning. Measured: a running `rclone sync` used 1 min 7 s of CPU across 70
minutes of wall clock, about 2%. Hetzner limits _concurrent connections_ —
`--checkers`/`--transfers` — not request rate.

### But make the loop un-blockable anyway

Fixing jackfruit does not fix the class. The design note's loop is still hostage
to whatever box turns pathological next.

The cheapest general guard needs no lane and no designation: **make the
scheduler skip a box whose per-box lock is held, instead of waiting for it.**
`sync_box` already builds `FileLock(..., timeout=0)` and then polls through
`acquire_lock_async` for `BOX_SYNC_LOCK_TIMEOUT` (600 s) before failing. A
skip-if-locked mode means the next tick simply syncs everything else while a
long box finishes.

Concurrent passes are already safe: per-box locks serialise the boxes, and the
only other contention is `refresh_boxyard_meta`, which takes the global lock for
about 100 ms (590 boxmetas parse in 20 ms plus a 149 KB write) against a 30 s
timeout. This also removes the red error lines that a fast META loop would
otherwise produce against a running DATA pass.

## Two corrections to the design note

**1. The skip filter is safe, but not for the stated reason.** The note says a
wrong "unchanged" verdict "needs a remote write that preserved `ModTime` — which
SFTP does not do." rclone _does_ preserve it: uploading a file stamped
`2021-03-04T05:06:07Z` produced exactly that `ModTime` on the remote. It is why
`doctor`'s `diverged-box` prefilter has to compare against the local record's
ULID timestamp with a 5-second window at all.

The filter is still safe, for a different reason: `BoxMeta.save()` rewrites the
file, so an edited boxmeta always carries a fresh mtime, and `Size` is compared
too. The only way to get a preserved mtime with different content is a push of a
byte-identical boxmeta, which is a no-op. Worth restating precisely, so nothing
later is built on the wrong premise.

**2. Worth taking while implementing the filter.** `SyncRecord.rclone_save`
calls `tempfile.mkstemp` afresh on every invocation, and a push saves the record
to remote and local in two separate calls — two temp files, two mtimes, drifting
under load (doctor measured a worst gap of 2.1 s across 750 records, hence the
5 s slack). Write the temp file once and copy it to both destinations and the
window collapses to SFTP's one-second mtime granularity, turning an empirical
constant into a bounded one.

## Also found, not part of this ticket

- **`sync_helper` stamps the completed record _after_ the transfer.** A file
  edited mid-push, after rclone walked its directory, is not transferred but is
  older than the record, so the box reads `SYNCED` and the edit is never pushed.
  Self-heals on an active box; a box that goes quiet loses it. Stamp with the
  push **start** time — strictly safe, and it is also what makes the race
  classifier below correct.
- **A live tree cannot finish a sync.** `20260825_kdo0rk__mosaic-v2` failed all
  three attempts in the 21:44 pass. Of the 782 distinct paths rclone named,
  **456 (58%) are not inside any generated directory** — real source being
  written by an agent mid-walk. Excludes shrink the target, they do not close
  the class. `--ignore-errors` is exactly backwards: its effect is to let rclone
  delete destination files despite errors. The fix is to classify failures —
  vanished-source races with no destination-side error record as success stamped
  with the push start time, while withheld deletions (`not deleting files as
  there were IO errors`) stay a hard failure. And the most effective mitigation
  is the boring one: race probability scales with walk duration, so every speed
  lever here is also a completion lever.
- **`rclone_path_exists` lists a path's parent**, so probing `boxes/<name>` lists
  all 590 box directories, 590 times per pass.
- **`/` on mymain is 98% full** (16 GB free, `~/dev` at 347 GB).

## Recommendation, in order

1. Add `paraglide/` to the excludes — 49% of jackfruit's daily churn. Config only.
2. `RCLONE_TPSLIMIT` 10 → 50 in both myrig loops — 5x on everything remote.
   Watch mymain's load for a day; revert is one variable.
3. Implement the design note as approved, with the skip filter's safety argument
   restated and the `rclone_save` temp-file fix taken alongside it.
4. Skip-if-locked in the scheduler, so no single box can hold the loop again.
5. Do **not** implement `compress`; leave the field out, or gate it in `doctor`.

Optional, not urgent: a one-off `rclone delete` for the artefacts stranded on the
remote by the new excludes (35,406 objects for jackfruit ≈ 66 min at tps 10,
13 min at 50). Adding an exclude deletes nothing — verified: rclone filters apply
to both sides, so already-pushed files under a now-excluded name go invisible and
stay forever.

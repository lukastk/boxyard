# Experiments plan — content-addressed DATA storage via restic

Throwaway prototyping behind
[`../../RESTIC-DATA-STORAGE-DESIGN-NOTE.md`](../../RESTIC-DATA-STORAGE-DESIGN-NOTE.md).
Same convention as the exec-bit plan next door: each experiment is a numbered
subdirectory, the only deliverable is **learnings**, and experiment code is
throwaway — production code gets rewritten once we are confident.

Ground rules held throughout: nothing was run against `~/.boxyard`, `~/dev` or
`~/g` except **reads**; every yard built for a test was a throwaway under `/tmp`;
the one remote write was scoped to `hetzner-box:boxyard-restic-probe/`, announced,
and purged with the deletion verified.

Status legend: `todo` · `in progress` · `done` · `skipped`.

---

## Group A — is restic the right tool, at this yard's scale?

### `00_baseline_and_yard_scale`

**Status:** done (2026-08-30) — measurements recorded in the design note and the
ticket's notes; no separate directory, the commands are all one-liners.

**Questions**
- Reproduce the ticket's jackfruit numbers rather than trusting them.
- How big is the yard *actually*, in objects and bytes, without an `rclone`
  recursive listing of 1.4M objects?

**Findings**
- The Hetzner restricted SSH shell answers this server-side in minutes:
  `ssh -p23 … "du --inodes --max-depth=1 ./boxyard/boxes"`. **593 boxes,
  1,376,982 inodes, 592.7 GiB.** Median box 196 inodes / 5.0 MiB; top box 20.3%
  of all inodes.
- **The ticket's jackfruit figure is stale.** 687,876 files / 7.52 GiB was before
  the 2026-08-28 exclude extension; it is now **134,551 files / 3.760 GiB**
  locally (280,061 inodes still on the remote, because excludes never clean it).
- restic on that corpus: 134,551 files + 11,863 dirs → **47 repo objects**;
  3.760 GiB → 638.6 MiB (dedup 3.4x, then compression 1.76x); first ingest 17 s,
  no-op backup 7 s.

### `01_index_scaling`

**Status:** done (2026-08-30)

**Questions**
- Does the index grow linearly in blob count, and what does a whole-yard index
  cost — the single most load-bearing unmeasured number in the design.
- What does a repo *open* cost, and does key derivation parallelise?

**Findings**
- **44.4 bytes of on-disk index per blob, perfectly linear** from 250k to 2M
  blobs. A whole-yard single repo would be ~1.8–2.0M blobs ≈ **80–90 MB index**,
  an 8 s cold fetch at the measured 11.8 MiB/s. Not hundreds of MB.
- The real per-operation cost is index *load*: a trivial `backup` took 2.18 s at
  2M blobs against 0.98 s at 53k.
- Repo open reproduced at ~780 ms (scrypt N=32768 r=8 p=7) — and it
  **parallelises near-linearly to 16x** with separate repos and separate caches.
  The previously reported cliff at 8 was contention from a shared repo and cache.
- Gotcha: restic calibrates scrypt at `init` against machine load, and there is
  no flag to pin it. Sixteen back-to-back repos got a mix of `p=6` and `p=7`.

### `02_cross_box_dedup`

**Status:** done (2026-08-30) — **this is the experiment that decided repo
granularity.**

**Questions**
- How much deduplication would per-box repos give up against one shared repo?
- Is the "many boxes are worktrees or clones" intuition worth anything in bytes?

**Method note worth keeping:** `restic backup --dry-run <many paths>` into an
empty repo reports the union's unique size **without storing anything**.
Validated against a real 117-box shared repo: predicted 4.785 GiB, actual
4.775 GiB (0.2%). This makes "what would a shared repo save" answerable for a
whole yard at the cost of reading, with no disk.

**Findings**
- 117 boxes ≤ 8 GiB: per-box 5.113 GiB vs shared 4.775 GiB → **6.6%**.
- All 121 local boxes (229,660 files, 266.59 GiB, 96% of mymain's bytes):
  per-box 68.635 GiB vs shared 63.821 GiB → **7.0%**.
- The saving is **not broad**: one box (`politick-ocr-eval`) supplies 303 of
  346 MiB in the sample. Excluding it, cross-box dedup is **0.8%**.
- The worktree clusters do deduplicate 74–97% each — and are 1.8 MiB boxes.
  True by count, false by bytes.
- Per-box repos still deliver **266.59 GiB → 68.635 GiB, 3.9x**.

---

## Group B — does it work against the real remote?

### `03_restic_backend_proto`

**Status:** done (2026-08-30) — 22/22 checks
([`FINDINGS.md`](03_restic_backend_proto/FINDINGS.md))

**Questions**
- Does restic reach hetzner-box through **boxyard's own** rclone config, and how
  does it compare with plain `rclone sync` on the same tree?
- Is the snapshot-ID skip filter real, and can it be read without the repo key?
- Is `restic diff` + `restore --include` actually faster *and correct*?
- Does a plain→restic conversion hurt a machine still running an older boxyard?

**Deliverable**
- `restic_backend.py` — a prototype DATA backend (push, pull, status, pointer,
  local state record).
- `e2e.py` — drives it through a throwaway two-machine yard: push, keyless
  pointer, cross-machine pull, skip-from-pointer, incremental diff pull, deletion
  propagation, both-sides-edit → CONFLICT, and conversion with an un-upgraded
  machine present.

**Findings** *(full writeup in [`03_restic_backend_proto/FINDINGS.md`](03_restic_backend_proto/FINDINGS.md))*
- **87x faster first push, 67x faster cold pull, 10-11x faster no-ops** against
  the real remote, on a 4,855-file box; 4,855 remote objects → 10.
- Snapshot filenames **are** the snapshot IDs, listable with no key.
- diff + targeted restore: **3.1–3.3 s** against 14–65 s for a full restore at
  jackfruit scale, and byte-identical.
- **Two traps that would otherwise have shipped:** `restore --include` does not
  delete; and restic records the pusher's **absolute path**, so `restic diff`
  across differing checkout roots reports everything as added+removed.
- **Conversion is safe only if `sync_records/<box>/data.rec` is deleted too** —
  otherwise an un-upgraded machine with local changes resurrects the plain
  `data/` on the remote.

---

## Group C — still to do

### `04_migration_dry_run` — `todo`

Convert a handful of *real* boxes (copied to a throwaway yard first) end to end,
including at least one remote-only box, and get a measured per-GiB conversion
rate rather than the current 11.8 MiB/s extrapolation. Also: what a partially
converted yard looks like to `doctor`.

### `05_prune_and_retention` — `todo`

Nothing in the design depends on it yet, but nothing has measured it either.
What does `forget`/`prune` cost on a per-box repo over SFTP, what does a stale
lock from a suspended machine actually do, and does `restic unlock` recover it
cleanly? Retention policy is an unmade decision: with none, every snapshot is
kept forever, which is cheap for text and not for the 89 GiB data boxes.

### `06_key_rotation` — `todo`

`restic key add`/`remove` across 590 per-box repos. Worth building the command
before it is needed rather than during an incident.

---

## Resolved decisions (2026-08-30)

- **One repo per box**, at `boxes/<index_name>/data.restic/`. Cross-box
  deduplication measured at 7.0% (0.8% excluding one outlier), against keyless
  skip-filtering, per-box independence, `delete` staying a directory removal,
  and a proportionate index.
- **A plain `boxes/<index_name>/data.snapshot` pointer** holding the snapshot ID
  and its source path, at depth 2 beside `boxmeta.toml`, so the bulk listing
  `--skip-unchanged-meta` already runs answers the DATA skip question at **zero
  additional remote calls**.
- **META and CONF stay plain.** Discovery rests on the depth-2 `boxmeta.toml`
  listing.
- **Pull is three-tier**: skip from the pointer → diff + targeted restore →
  full subpath restore, with the full restore taken whenever the diff cannot be
  trusted. Every fallback costs time, never correctness.
- **`restore <snap>:<source> --target <dir>`** is the restore form, so a box is
  restorable onto a machine whose checkout root differs. `--include` anchors to
  the subpath with a **leading slash**.
- **`write_owner` stays optional.** restic tolerates concurrent writers; only
  `prune` needs exclusivity, and `prune` never runs in the sync loop.
- **One fleet password**, fetched once per process into `RESTIC_PASSWORD` — not
  `--password-command`, which reruns `secret get` (0.77–1.51 s) per invocation.
- **Policy governs creation, not conversion.** `storage_format` defaults to
  `restic`; existing boxes change format only through an explicit, verified,
  per-box `boxyard convert`.

## Still to decide

- Retention: what `forget` policy, if any, and per box or per group.
- Where the second, independent copy of the restic password lives.
- Whether the DATA cadence should shorten back toward META's once boxes are
  converted (it should, but with numbers, after migration).
- Whether `doctor` should report boxes pushed from two different absolute paths,
  which permanently lose incremental pulls until their checkout roots align.

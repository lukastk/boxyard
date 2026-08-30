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
  1,376,982 inodes, 1,107.0 GiB logical (592.8 GiB on disk).** Median box 196
  inodes / 3.3 MiB; top box 20.3% of all inodes.
- **`--apparent-size` is MANDATORY and its absence is a 2-3x error.** The first
  pass here used plain `du`, which reports allocated blocks, and the storage box
  compresses transparently (~1.9x on box data, ~3.0x on backup residue). Plain
  `du` on `sync_backups` said 38.55 GiB where the logical size is 116.40 GiB —
  and `du --apparent-size` agrees with an independent `rclone size` to three
  decimal places, which is what validates the method. `du --count-links` gives
  the same 38.55 GiB, ruling out hardlinks. The error is not even one-directional:
  on many-tiny-file trees, block rounding makes plain `du` 1.05-2.47x too HIGH.
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

**Status:** done (2026-08-30) — 30/30 checks
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
- **restic carries Unix mode natively** (755/644/775 exact), so the exec-bit
  manifest is redundant for restic-backed boxes — and `backup --exclude` anchors
  by the OPPOSITE rule to `restore --include`.
- **A forgotten base snapshot made a clean replica report CONFLICT.** Found and
  fixed: the local state record now carries `synced_at_unix`, and modification
  falls back to the mtime test the plain backend already uses.

---

## Group C — still to do

### `04_migration_dry_run` — `todo`

Convert a handful of *real* boxes (copied to a throwaway yard first) end to end,
including at least one remote-only box, and get a measured per-GiB conversion
rate rather than the current 11.8 MiB/s extrapolation. Also: what a partially
converted yard looks like to `doctor`.

### `05_prune_and_retention` — `in progress`

**Substantially answered 2026-08-30**, and it changed three design decisions.

**Snapshot count does NOT degrade anything.** One repo grown in place:

| snapshots | `backup` | `snapshots` | `forget --dry-run` | `snapshots/` on disk |
|---|---|---|---|---|
| 0 | 0.86 s | 0.78 s | 0.82 s | 450 B |
| 100 | 0.80 s | 0.79 s | 0.78 s | 50 KB |
| 500 | 0.83 s | 0.83 s | 0.82 s | 248 KB |
| 823 | — | — | 0.90 s | ~410 KB |

Flat; all dominated by the ~780 ms key derivation. Combined with the fact that
the skip filter reads the depth-2 `data.snapshot` pointer rather than listing
`snapshots/`, snapshot growth cannot attack the skip filter. **Retention is
justified by STORAGE, not by listing cost** — which points at a periodic pass
rather than a hot-path one.

**`--keep-within` expires; it does not thin.** Against 823 snapshots all created
the same day (an actively-edited box, or any box if the DATA cadence shortens):

| policy | keeps | removes |
|---|---|---|
| `--keep-within 7d --keep-daily 90 --keep-last 10` | **823** | **0** |
| `--keep-within 90d` | **823** | **0** |
| `--keep-last 10 --keep-hourly 24 --keep-daily 30 --keep-weekly 13 --keep-monthly 3` | **11** | 812 |
| that ladder **plus** `--keep-within 1d` | **823** | **0** |

restic's policy is a UNION, so any `--keep-within` re-admits its whole window and
destroys a working ladder. A retention scalar must expand to a pure `--keep-N`
ladder. (`--keep-within` IS measured relative to the newest snapshot, not to now
— verified on a repo back-dated 200 days, `--keep-within 90d` kept 2 of 6 — so a
dormant box never loses its history to the passage of time. That part is fine;
it just does not thin.)

**`forget` takes an exclusive lock, but `--retry-lock` survives it.**
`unable to create lock in backend: repository is already locked exclusively`.
A `backup --retry-lock 30s` colliding with a `forget` succeeded 3 trials of 3,
waiting 5.83 / 5.88 / 5.85 s. So an earlier "forget cannot run in the sync loop"
was too strong — it is survivable. It is excluded on COST instead (below).

**Maintenance cost, per box, over the real remote** (scoped write to
`boxyard-restic-probe2/`, purged and verified absent):

| operation | time |
|---|---|
| repo open (`cat config`) | 2.02 s |
| `forget --dry-run` | 2.18 s |
| `forget` (real) | 2.87 s |
| `prune --max-unused 10%` (repacking) | 4.94 s |
| `prune --max-unused 10%` (**nothing to do**) | 3.73 s |
| *no-op push, for comparison* | 3.1 s |

So `forget` after every push nearly doubles push cost, and a `prune` with nothing
to do is *not* free — gate `prune` on whether this run's `forget` actually
removed anything, which is a free local signal.

Whole-yard weekly pass: `forget` over 594 boxes ≈ 28 min serial / ~3.5 min at
concurrency 8; `prune` ≈ 37 min / ~4.6 min.

Still open: stale-lock recovery from a machine that suspended mid-operation
(pocket4 goes dark for days) and whether `restic unlock` recovers cleanly; and
the retention ladder numbers themselves, which are Lukas's call.

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
- **The exec-bit manifest is skipped for restic-backed boxes.** restic carries
  mode natively and exactly (755/644/775 all round-tripped; the manifest can only
  approximate, and its v1 is additive-only). `_utils/perms.py` stays for plain
  boxes. The push excludes `<data_path>/.boxyard-perms.json` so a converted box
  does not carry one into every snapshot.
- **`sync_backups/` stops for restic-backed DATA by construction** — that path no
  longer calls `rclone sync`. META and CONF keep theirs.
- **Neither `forget` nor `prune` ever runs in the sync loop.** Both take
  exclusive locks (measured).

### `07_canonical_path` — `done` (2026-08-30)

**Question:** can a fixed canonical path make every machine record the same
snapshot path, removing the whole absolute-path problem rather than working
around it?

**Findings — yes, on 5 of 6 machines.**
- A symlink as the FINAL path component archives the LINK: a 0-file snapshot,
  silently. As an INTERMEDIATE component it is recorded unresolved AND traversed.
  So the shape is `<canon>/<index_name>/<index_name>`, link then real directory.
- `--ignore-inode` is required: two machines never share inodes, so a restored
  replica reports `changed=2 unmodified=0` without it and `changed=0
  unmodified=2` with it, while a genuine edit is still seen.
- Verified on macOS (macstudio, via a throwaway restic binary, removed
  afterwards): `/tmp` is itself a symlink to `private/tmp` there and the path is
  still recorded verbatim.
- `/tmp` is `drwxrwxrwt` on mymain, ideapad and macOS, so the root must be
  created 0700 and validated as a real, self-owned directory before use.
- Atomic re-point costs 0.033 ms.
- termux cannot write any fixed absolute path (untrusted_app uid; `/tmp` is 0771
  owned by `shell`, `/var/tmp` and `/data/local/tmp` refused) — but it **does not
  run boxyard**: no binary, no `~/.boxyard`, and its three `~/dev` entries are
  plain git repos. Verified. So it is not a constraint on this design.

**Decision:** adopt the canonical path — it is the normal path for the whole live
fleet, which is entirely macOS and Linux. KEEP `parent_is_usable` and
`PullMode.FULL_PATH_MISMATCH`, not for termux but because a forgotten parent
snapshot (permanent, routine once retention ships) and an unusable canonical root
(defence in depth) both reach them.

---

## Still to decide

- Retention: the ladder numbers. Proposed `retention = "90d"` expanding to
  `--keep-last 10 --keep-hourly 24 --keep-daily 30 --keep-weekly 13
  --keep-monthly 3`, with `cold` on `"365d"`, as a `retention` dimension on the
  existing policy axis. **Must be settled before the first box is converted** —
  with no policy, repos grow without bound.
- Whether to build point-in-time recovery as a command. It becomes possible;
  deliberately out of scope here.
- The 116.4 GiB (logical) of `sync_backups` residue: 1,186 orphaned directories back to
  2025-11, against exactly 1 live incomplete sync on mymain. A separate ticket —
  restic neither causes nor fixes it, and it is NOT the same problem as the
  stranded objects the exclude extension left behind.
- Where the second, independent copy of the restic password lives.
- Whether the DATA cadence should shorten back toward META's once boxes are
  converted (it should, but with numbers, after migration).
- Whether `doctor` should report boxes pushed from two different absolute paths,
  which permanently lose incremental pulls until their checkout roots align.

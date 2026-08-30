# Content-addressed DATA storage via restic — design note

Status: **proposed, nothing implemented in the package.** A working prototype
lives at `_dev/experiments/restic/03_restic_backend_proto/` (22/22 end-to-end
checks). This replaces how the primary copy of everything is stored, so nothing
ships before Lukas agrees to the approach.

All numbers here were measured on mymain, 2026-08-30, restic 0.18.0, rclone
v1.75.0, against the live Hetzner storage box through boxyard's own rclone
config. Where the machine was under load at the time, it says so.

---

## The result that decides the whole thing

Same tree, same remote, same rclone config. `20260601_zbl55q__sesh--major-review`,
4,855 files / 49.6 MiB — a p97 box by file count, not a pathological one.

| | restic | plain rclone sync | ratio |
|---|---|---|---|
| first push | 4.6 s | 402.5 s | **87x** |
| no-op push | 3.1 s | 30.7 s | **10x** |
| cold pull | 3.4 s | 227.1 s | **67x** |
| no-op pull | 2.7 s | 30.8 s | **11x** |
| objects on the remote | 10 | 4,855 | 486x |
| bytes on the remote | 19.1 MiB | 49.6 MiB | 2.6x |

The mechanism is not compression. It is that boxyard's unit of work stops being
the file. Every cost today — the status probe, the listing, the transfer
decision — is one or more SFTP round trips **per file**, and a round trip to this
box costs 0.67 s no matter how small the file. restic moves whole packs, so the
remote sees tens of objects where it used to see hundreds of thousands.

At yard scale, measured server-side (see *Measuring the remote cheaply* below):
`boxyard/boxes` holds **1,376,982 inodes across 592.7 GiB in 593 box
directories** — the registry lists 594, one of which is registered locally and
not yet on the remote. Restic-backing the yard would replace essentially all of
those objects with a few thousand packs.

---

## Recommendation, in one paragraph

**One restic repo per box**, at `boxes/<index_name>/data.restic/`, with a plain
`boxes/<index_name>/data.snapshot` pointer beside `boxmeta.toml`. META and CONF
stay plain files, untouched. `storage_format` becomes a `_sync_policy`
dimension defaulting to **`restic`**, and it governs box CREATION; existing
boxes carry their actual format in `boxmeta.toml` and only change it through an
explicit, verified, per-box `boxyard convert`. `write_owner` stays optional.

---

## 1. Repo granularity — per box

This was the open question, and it is now measured rather than argued.

### Cross-box deduplication is worth 7%

Method: ingest every box into its own repo and sum `stats --mode raw-data`, then
ingest the same boxes into one shared repo and compare. Validated a zero-storage
shortcut first — `restic backup --dry-run <many paths>` into an empty repo
reports the union's unique size without writing anything; against the real
117-box shared repo it predicted 4.785 GiB where the truth was 4.775 GiB (0.2%).

| corpus | per-box repos | one shared repo | saving |
|---|---|---|---|
| 117 boxes ≤ 8 GiB (208,673 files, 11.05 GiB) | 5.113 GiB | 4.775 GiB | **6.6%** |
| all 121 local boxes (229,660 files, 266.59 GiB) | 68.635 GiB | 63.821 GiB | **7.0%** |

Two consistent measurements, the second covering 96% of mymain's bytes.

And the 7% is not broad. In the 117-box sample, **one box supplies 303 MiB of
the 346 MiB**: `politick-ocr-eval`, 99.6% of whose content duplicates another
box. Remove it and cross-box dedup is **0.8%**.

The "many boxes are worktrees or clones" intuition is true by count and false by
bytes. The `ituc-*` worktree cluster deduplicates 74–97% *each* — and they are
1.8 MiB boxes, so the whole cluster saves about 10 MiB. The 4.8x deduplication
measured inside jackfruit is **within-box**, and per-box repos keep all of it.
On the current corpus, per-box repos still give **266.59 GiB → 68.635 GiB, 3.9x**.

### What per-box buys, and it is a lot

- **The skip filter needs no key.** A repo's `snapshots/` filenames *are* the
  snapshot IDs — verified: a new snapshot adds exactly one file whose name is
  the full ID. In a per-box repo that directory is unambiguously about one box.
  A shared repo would hold 590+ snapshot files with nothing outside the
  encryption saying which box each belongs to, so it would need the pointer-file
  machinery anyway — which per-box gets for free.
- **`boxyard delete` stays a directory removal.** In a shared repo it becomes
  `forget` + `prune`, which rewrites packs, needs an exclusive lock, and has to
  be coordinated across five machines. `prune` is where these systems eat data.
- **Per-box independence is preserved.** It is the structural property that
  makes per-box locks, `write_owner`, tombstones and deletion simple today.
- **The index loaded is the box's, not the yard's.** See below.
- **Failure is contained.** One corrupted repo is one box.

### The two arguments against it both evaporated under measurement

**Key derivation is not a problem.** Repo open is ~780 ms (reproduced exactly:
771/808/806/784/780 ms, scrypt N=32768 r=8 p=7), but it parallelises:

| concurrency | wall | per open | speedup |
|---|---|---|---|
| 1 | 699 ms | 699 ms | 1.0x |
| 2 | 726 ms | 363 ms | 2.1x |
| 4 | 779 ms | 195 ms | 4.0x |
| 8 | 850 ms | 106 ms | 7.3x |
| 16 | 965 ms | 60 ms | 12.9x |

Near-linear to 16, with separate repos and separate cache dirs. The cliff at 8
reported earlier was contention from every process sharing one repo and one
cache. 590 cold opens at concurrency 16 is ~29 s — and the pointer skip filter
means a normal pass opens a handful, not 590.

**Index size is not a problem either way.** Measured by ingesting unique
small files into one repo in stages:

| blobs | index on disk | bytes/blob |
|---|---|---|
| 250,636 | 11,117,679 | 44.4 |
| 501,273 | 22,241,485 | 44.4 |
| 1,002,536 | 44,473,842 | 44.4 |
| 2,005,052 | 88,940,455 | 44.4 |

Perfectly linear, no super-linear term. A whole-yard single repo would be
roughly 1.2M files plus ~600k extra chunks from large files ≈ 1.8–2.0M blobs
before dedup, i.e. an **~80–90 MB index**. At the measured 11.8 MiB/s to this
remote that is an 8 s cold fetch, once per machine. It is *not* hundreds of MB
and it does not rule out the shared repo.

What it does cost is per-operation: at 2M blobs a trivial `backup` took **2.18 s**
against **0.98 s** on a 53k-blob repo — index load adds ~1.5 s to *every*
operation on a shared repo. Per-box repos pay this in proportion to the box.

### Verdict

Per-box costs 7% of stored bytes — 0.8% excluding a single outlier — and buys
keyless skip-filtering, independence, cheap deletion, and a proportionate index.
**Per box.**

> Gotcha to record: restic calibrates scrypt at `init` against the machine's
> speed at that moment, and there is no CLI flag to pin it. Sixteen scratch
> repos created back to back got `p=6` (twelve) and `p=7` (four) purely from
> load variation. 590 per-box repos means 590 independently calibrated key
> parameters. Cosmetic, but "780 ms" is a distribution, and a repo created on
> macstudio opens more slowly on pocket4.

---

## 2. Pull strategy

The ticket's negative result — "a no-op restore is slower than a cold one, so
push is excellent and pull is not" — is **half right, and the half that is wrong
matters.**

Measured over the real remote (4,855-file box): cold restore 3.4 s, no-op
restore **2.7 s** — faster than cold, and 11x better than the rclone pull it
replaces. The ticket's 102/161/167 s figures came from a much larger box against
a local repo, where the dominant term is restic reading every destination file
to compare.

The correct statement:

```
push cost = O(changed bytes)
pull cost = O(destination tree size)   -- not O(changed), but also not
                                          O(remote objects), which is what
                                          rclone pays today
```

Only push gets the asymptotic win. Pull still wins by an order of magnitude
because it stops paying per-file remote round trips, and round trips — not local
reads — are what makes today's sync take hours.

### The design has three tiers

**Tier 1 — do nothing, for almost every box, at zero marginal remote cost.**
Write the current snapshot ID and its source path to `boxes/<box>/data.snapshot`,
a tiny plain file at depth 2 beside `boxmeta.toml`. The bulk listing that
`sync-missing-meta` and `--skip-unchanged-meta` **already run** then answers
"did this box's DATA move" for all 590 boxes *in the same single call it already
pays for*. This is structurally identical to `_sync_policy.meta_boxes_needing_sync`
and reuses its (ModTime, Size) comparison and its safety argument. The repo's own
`snapshots/` directory remains the truth; the pointer is a hint, and a stale hint
costs one repo open, never correctness.

**Tier 2 — targeted restore, when the pointer moved.** Measured at jackfruit
scale (134,551 files / 3.760 GiB, v1→v2 differing by 50 edited source files):

| | time |
|---|---|
| cold full restore | 24.1 s |
| full restore over an identical tree | 14.3–55.8 s |
| full restore with 50 stale files | 16.3–64.8 s |
| `restic diff` | 2.3 s |
| `restore --include` (50 paths) | 0.9 s |
| **diff + targeted restore, 3 reps** | **3.1 / 3.3 / 3.1 s** |

and the targeted result is **byte-identical to the full restore** (`diff -r`
clean). The full-restore spread is wide because the machine was at load 6.6 from
the live supervisor; the targeted figure is stable because it barely touches the
disk.

**Tier 3 — full restore**, taken whenever tier 2 cannot be trusted. See below.

### Two correctness requirements the measurements forced

**`restore --include` does not delete.** Verified: a file absent from the newer
snapshot survives an `--include` restore and is only removed by a full
`restore --delete`. So the pull must apply the diff's `-` lines as explicit local
deletions. Without this, a file deleted on another machine silently comes back —
the exact class of bug tombstones exist to prevent.

**restic records the pusher's absolute path, and `restic diff` compares by
absolute path.** Two snapshots of *byte-identical* content backed up from
different paths diff as everything-added/everything-removed (`Files: 2 new, 2
removed, 0 changed` — while `Data Blobs: 0 new, 0 removed`, so deduplication is
perfect and only the *diff* is useless). This is live here: mymain already has
two checkout roots (`~/dev`, `~/hetzner_volume/boxes`) and the Macs use
`/Users/lukastk`. A naive diff-driven pull would delete the tree and restore it.

Four normalisation routes were tested and all are dead: `backup --set-path` does
not exist; `rewrite` changes host and time but not paths; `cd <box> && backup .`
still records the absolute path; backing up a symlink at a fixed canonical path
records the fixed path but archives *the symlink* (0 B snapshot). A bind mount
would work and needs root — rejected.

The fix, verified:

```
restic restore <snap>:<source_path> --target <box data dir>
```

places the snapshot's **contents** at the target, so the local checkout path need
not match the pusher's. With that form `--include` is anchored to the subpath by
a **leading slash**:

| pattern | result |
|---|---|
| `--include /coordination/FLOOR-REPORT.md` | exactly that file — correct |
| `--include coordination/FLOOR-REPORT.md` | works, unanchored |
| `--include package.json` | 98 unrelated matches — trap |
| `--include /abs/path/...` | 0 files — trap |

So the pointer records the snapshot ID **and its source path**, and tier 2 is
used only when base and target share a source path. When they differ, fall back
to a full subpath restore: more work, never a wrong result. `doctor` should
report a box pushed from two different paths, because that box permanently loses
incremental pulls until its checkout roots are aligned.

---

## 3. Key management

**One password for the whole fleet, one key per repo, stored in 1Password.**

- `config.toml` gains `restic_password_command`, e.g.
  `secret get BOXYARD_RESTIC_PASSWORD`. The config is generated by myrig from a
  Jinja template and lives on every machine — the password must never be in it.
- **Fetch once per process**, pass via `RESTIC_PASSWORD` in the child
  environment. Not restic's `--password-command`, which reruns it per
  invocation: `secret get` measured 0.77–1.51 s, so per-box that is ~8 minutes of
  1Password round trips per 590-box pass, for a value that does not change.
- **One password, not one per machine.** restic tries keys in turn, so N keys
  costs up to N × 780 ms per open, and recovery gets N times more fragile.

**No circular dependency.** `secret` resolves to
`~/mysetup/myrig/home/.mybin/…` and the service-account token to
`~/.config/op/token`. Neither is inside a box, and `~/mysetup` is not a checkout
root. Verified explicitly.

**Recovery.** Losing the password loses everything, so it needs a second,
independent home. 1Password is already the rig's single source of truth and is
itself backed up, but it is one system. The cheap insurance is that mymain
already runs a `borg-backup` supervisor job against a separate `backups/` tree on
the same storage box: keep the restic password in that path too, and the two
systems fail independently. This should be settled before the first box is
converted, not after.

**Rotation** is `restic key add` / `restic key remove` per repo — which with
per-box repos means 590 repos. Rotation is therefore a real operation with a
real cost, and worth writing a command for before it is needed rather than
during an incident.

---

## 4. Conflict detection

Snapshot IDs are a strictly better substrate than the ULID pair, because a
snapshot history is linear and identified rather than timestamped.

| today (plain) | restic-backed |
|---|---|
| remote `sync_records/<box>/data.rec` (ULID) | `boxes/<box>/data.snapshot` pointer |
| local `sync_records/<box>/data.rec` (ULID) | `~/.boxyard/restic_state/<box>/data.json` |
| local mtime walk vs record timestamp | `backup --dry-run --parent <snap>` |
| ULIDs match + not modified → `SYNCED` | pointer == local record + not modified → `SYNCED` |
| ULIDs match + modified → `NEEDS_PUSH` | pointer == local record + modified → `NEEDS_PUSH` |
| remote ULID newer + not modified → `NEEDS_PULL` | pointer ≠ local record + not modified → `NEEDS_PULL` |
| remote ULID newer + modified → `CONFLICT` | pointer ≠ local record + modified → `CONFLICT` |
| `SYNC_TO_REMOTE_INCOMPLETE` | **gone** — see below |
| `SYNC_FROM_REMOTE_INCOMPLETE` | kept (a restore can still be interrupted) |

Two things genuinely improve:

- **The interrupted-push state disappears.** A restic snapshot exists or it does
  not; an interrupted `backup` leaves orphaned packs and no snapshot, which is
  wasted space, not a corrupt remote. The whole `SYNC_TO_REMOTE_INCOMPLETE`
  machinery — matching incomplete ULIDs on both sides to prove which machine owns
  the interrupted sync — has nothing left to describe for DATA.
- **Change detection stops being a mtime walk.** `backup --dry-run --parent`
  reuses restic's own detection instead of `check_last_time_modified`.

  > **Trap, found by the prototype:** a perfect no-op reports `Dirs: 0 new, 1
  > changed` — the root directory's own metadata is re-read. Consulting
  > `dirs_changed` makes every box permanently `NEEDS_PUSH` and silently
  > disables the entire skip filter. Use `files_new`, `files_changed`,
  > `dirs_new` only.

What does **not** change: a `CONFLICT` is still a human's problem. restic gives
no merge. The one improvement is that the losing side's work is never destroyed —
it can be pushed as its own snapshot and recovered from history, which the plain
backend cannot offer.

---

## 5. Ownership — `write_owner` stays optional

**No, `write_owner` should not become mandatory.** restic makes unowned boxes
*safer*, not less safe.

Measured: two concurrent `restic backup` runs into one repo, with separate cache
directories (two machines simulated), both exit 0, both snapshots are recorded,
no leftover locks. `backup` takes a non-exclusive lock. Where today two machines
pushing the same box is a genuine race with a backup directory as the only
safety net, under restic it produces two valid snapshots and a pointer race that
resolves last-write-wins — and the loser's work is still in the repo.

Forcing ownership on all 594 boxes would mass-assign state to 321 boxes nobody
chose to claim, which is precisely what `_ownership`'s "unowned means
unrestricted" rule exists to prevent.

Where single-writer *does* matter is **`prune`**, which takes an exclusive lock
and rewrites packs. So:

- `prune` and `forget` are **never** part of the sync loop. They are explicit,
  rare, opt-in commands.
- For an owned box, only the owner prunes.
- For an unowned box, pruning is a deliberate act by whoever runs it, and the
  exclusive lock is the coordination mechanism — with `restic unlock` as the
  documented recovery from a machine that suspended mid-operation, which on this
  fleet (pocket4 offline for days) will happen.

Retention is a separate decision that should be made explicitly rather than
inherited: `forget --keep-within` with no policy at all means every snapshot is
kept forever, which is cheap for text and not for the 89 GiB data boxes.

---

## 6. Migration

**327 GiB exists only on the remote** (592.7 GiB total minus mymain's 266.6 GiB
checkout, modulo what the Macs hold), and there is no server-side shortcut: the
Hetzner restricted shell offers `du`, `df` and `borg 1.2.9`, but `restic` and
`rclone` are "Command not found". Confirmed. Converting a remote-only box means
download and re-ingest by some machine.

At the measured 11.8 MiB/s that is roughly **8 hours down and 2 hours back up**,
one-off, per-box resumable, and runnable in the background over days. That is
the accepted cost, and per-box repos make it *per box* — no flag day, no
fleet-wide state, and it can stop and resume at any box boundary.

### The conversion procedure, and why the order matters

This was tested against an un-upgraded machine in a throwaway two-machine yard.

1. push the repo to `boxes/<box>/data.restic/`
2. **verify a restore is byte-identical** before destroying anything
3. purge `boxes/<box>/data/`
4. **delete `boxyard/sync_records/<box>/data.rec`**
5. write `boxes/<box>/data.snapshot`; set `storage_format` in `boxmeta.toml`

Step 4 is the gate, and skipping it is the failure mode:

| what was done | what an un-upgraded machine does |
|---|---|
| remove `data/` only; non-owner, no local changes | reports `SYNCED`, does nothing. Safe, but **silent** — the box stops syncing there with no signal |
| remove `data/` only; **unowned** box, local changes | **resurrects the plain `data/` on the remote.** Local data intact, repo intact, but the box now exists in both formats and diverges with nothing reporting it |
| remove `data/` **and** `data.rec` | **refuses loudly**: "Local sync record exists, but remote path does not exist". Nothing resurrected, local data intact |

The third row is the gate. It works because it drives `get_sync_status` into its
existing `ERROR` branch, which `sync_helper` raises on for anything but
`--force`. **No fleet-wide version negotiation, no heartbeat mechanism, no flag
day** — the gate is a state old code already refuses on.

The unowned+local-changes row is not hypothetical: 321 of 594 boxes are unowned
today.

**The cost of the gate** is that an un-upgraded machine then logs one error per
converted box per pass — the "cries wolf" pathology v0.4.x spent a week removing.
It is bounded by the migration window, and new boxyard must recognise this exact
state and render it as its own `SyncCondition` (alongside `WRITE_DENIED` and
`LOCAL_STORAGE`), not as an error.

### Order of migration

1. New boxes are restic from creation — free, and it is the default.
2. Boxes checked out on an upgraded machine convert on request, cheaply.
3. Remote-only boxes convert in a background pass, most-used first.
4. Archived boxes convert last, or never — they are the ones that benefit least
   and cost most, and `plain` remains a legitimate permanent state.

---

## 7. `restic` as the default, and how the switch is gated

Lukas: *"we need to make sure that `restic` is the default sync policy once it's
been implemented."*

`storage_format` becomes a third `_sync_policy` dimension, resolved by the
existing per-dimension rule — box `conf/sync.toml` → matched group policy →
default — with the default `restic` for non-`local` storage locations and
`plain` for `local` ones, where the per-file transaction cost that motivates all
of this does not exist.

**But policy governs creation, not conversion.** The distinction is the whole
safety story:

- `resolve_policy` gives the **intended** format. Default `restic`.
- `boxmeta.storage_format` records the **actual** format, written at creation and
  changed only by `boxyard convert`.
- `doctor` reports boxes where intended ≠ actual, and names `boxyard convert` as
  the fix.

So editing `config.toml` never reformats 590 boxes on the next pass. A config
edit that silently rewrites the primary copy of everything is exactly the defect
the removed `compress` field had, and it must not be reintroduced in a different
shape.

**What gates the switch, concretely:**

1. **Nothing converts implicitly.** An existing box stays `plain` until someone
   runs `boxyard convert` on it. This alone means a partly-upgraded fleet is
   never surprised.
2. **A new box created on an upgraded machine is restic immediately**, and an
   un-upgraded machine that discovers it finds `boxmeta.toml` (which it can read)
   and no `data/` and no `data.rec` — the refusal state above. It reports an
   error and touches nothing. Verified.
3. **`boxyard convert` refuses unless this machine holds the box and can verify
   the restore**, which is step 2 of the procedure.
4. **`doctor` gains a check** naming boxes whose format the local boxyard cannot
   handle, so an un-upgraded machine has one clear message rather than a per-pass
   mystery.
5. **pocket4 is offline for days at a time.** Nothing above requires it to be
   reachable, because there is no negotiation — only a refusal state. When it
   comes back and is upgraded, it converges.

The one thing this design deliberately does **not** do is make the default flip
convert anything. `plain` must remain reachable for the migration window, for
`local` storage locations, and as a rollback path, and the honest way to keep it
reachable is to keep conversion explicit in both directions.

---

## 8. Interaction with the sync-cadence work

The cadence work (`--due-only`, `--skip-unchanged-meta`, `_sync_policy`) landed
this week and is not switched on. It survives essentially intact, and gets
better.

- **`_sync_policy.resolve_policy`** gains `storage_format` as a third dimension.
  No new mechanism; `BOX_OVERRIDABLE` grows by one entry.
- **`--skip-unchanged-meta` generalises to `--skip-unchanged`.** The bulk
  depth-2 listing already fetches everything at `boxes/*/`; adding
  `data.snapshot` to its filter means the *same single call* answers the skip
  question for both META and DATA. The check record grows a `remote_snapshot`
  field alongside `remote_modtime`/`remote_size`, and
  `meta_boxes_needing_sync` gains a DATA sibling with identical shape. **Zero
  additional remote calls per pass.**
- **`--due-only` is unchanged** in mechanism, and much less important in effect.
  The 6h DATA cadence exists because a DATA pass is ruinously expensive; a no-op
  restic DATA check costs ~3 s against 30–400 s today. Once boxes are converted,
  the honest move is to **shorten the DATA cadence back toward META's**, which
  removes the "up to 6h of work living on one disk" trade the cadence note
  explicitly accepted. That is a decision to take *after* migration, with
  numbers, not now.
- **The 10-hour box stops holding the fleet hostage.** That wrinkle is listed as
  "known, non-architectural" in the cadence note; this removes its cause.
- **`compress` staying removed is confirmed correct.** Compression is a backend
  property: measured 1.76x on jackfruit on top of 3.4x deduplication, with no
  knob and no per-box decision.

---

## What I would build first — and what I would not

**Build, in this order:**

1. **`_restic.py`** — repo URL, env, push, pull, status, pointer read/write, and
   the local state record. Modelled on the prototype, which is already the right
   shape. Pure functions over paths and IDs, no box semantics, mirroring how
   `sync_helper` is layered under `sync_box`.
2. **`storage_format` in `_sync_policy` and `boxmeta`**, with `doctor` reporting
   intended ≠ actual. No behaviour change yet: every existing box is `plain`, so
   the yard is untouched.
3. **`boxyard convert`** — the five-step procedure, verify-before-destroy, one
   box at a time, refusing unless this machine holds the box.
4. **The DATA branch in `sync_box`**, dispatching on the box's actual format. The
   plain path stays exactly as it is.
5. **`--skip-unchanged` extending the existing bulk listing.**
6. Only then: `restic` as the resolved default for new boxes.

**Deliberately not building:**

- **A shared repo per storage location.** Measured at 7% of stored bytes, for
  keyless skip-filtering, per-box independence, cheap deletion and a
  proportionate index.
- **Restic-backing META or CONF.** `sync-missing-meta`'s one bulk `lsjson` over
  `boxes/*/boxmeta.toml` at depth 2 is what box discovery rests on. Non-negotiable
  without a replacement discovery mechanism, and there is no reason to want one.
- **Automatic conversion of existing boxes**, in either direction.
- **`prune`/`forget` anywhere near the sync loop.**
- **Our own chunker.** Unchanged: chunking is a weekend, a correct garbage
  collector is not.
- **The Go port.** Python first. `feat/go-rewrite` is 24 commits behind main and
  has no notion of named checkout roots; two of its tests fail for that reason
  alone and it is not this ticket's business.

---

## Appendix: measuring the remote cheaply

Worth keeping independently of this design. The Hetzner Storage Box exposes a
restricted SSH shell on port 23 which accepts **one command per connection** and
offers `du` and `df` (but not `ls`, `find`, `restic` or `rclone`). Key auth is
already set up.

```bash
ssh -p 23 u508472@u508472.your-storagebox.de "du --inodes --max-depth=1 ./boxyard/boxes"
ssh -p 23 u508472@u508472.your-storagebox.de "du -k --max-depth=1 ./boxyard/boxes"
```

Server-side, no per-file network round trips, minutes rather than the hours an
`rclone` recursive listing of 1.4M objects would take. This is how every number
in *The result that decides the whole thing* about yard scale was obtained.

Current shape of the yard, for reference:

| | |
|---|---|
| boxes | 593 |
| inodes (files + dirs) | 1,376,982 |
| bytes | 592.7 GiB |
| median box | 196 inodes, 5.0 MiB |
| p90 box | 3,034 inodes, 557 MiB |
| largest box | 280,061 inodes (`jackfruit-hq-mymain`) |
| skew | top 1 box = 20.3% of inodes; top 10 = 49.2%; top 50 = 81.9% |
| whole store | 632 GiB, including 39 GiB of `sync_backups` |

> One correction to the ticket's premise while we are here. It cites
> jackfruit-hq-mymain as 687,876 files / 7.52 GiB. Measured today with the
> **current** default excludes it is **134,551 files / 3.760 GiB** locally, and
> 280,061 inodes on the remote. The exclude list was extended on 2026-08-28
> (myrig `44c7706`) with `.svelte-kit/`, `paraglide/`, `target/` and others,
> which cut the box roughly 5x before restic was involved at all. restic's win is
> still large — the baseline it is measured against has simply moved, and the
> remote still carries the stranded objects because an exclude never cleans the
> remote.

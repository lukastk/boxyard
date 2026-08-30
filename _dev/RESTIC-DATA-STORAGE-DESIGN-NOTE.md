# Content-addressed DATA storage via restic — design note

Status: **proposed, nothing implemented in the package.** A working prototype
lives at `_dev/experiments/restic/03_restic_backend_proto/` (30/30 end-to-end
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
`boxyard/boxes` holds **1,376,982 inodes across 1,107.0 GiB of logical data,
occupying 592.8 GiB on disk**, in 593 box directories — the registry lists 594,
one of which is registered locally and not yet on the remote. Restic-backing the
yard would replace essentially all of those objects with a few thousand packs.

> The two byte figures differ because **the storage box's filesystem compresses
> transparently, ~1.9x on this data**. That distinction is load-bearing here and
> it is easy to get wrong — an earlier revision of this note quoted the on-disk
> numbers as though they were logical ones. See *Measuring the remote cheaply*
> for the trap, and §1 for the one conclusion it changes.

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
On the current corpus, per-box repos still give **266.59 GiB → 68.635 GiB,
3.9x** in logical bytes — which is what determines how much has to cross the
wire.

**But the saving in REMOTE QUOTA is smaller, and the note previously overstated
it.** The storage box compresses plain trees transparently — measured 1.91x
across the remote copies of these same 121 boxes, and 1.87x yard-wide — while
restic's output is encrypted and therefore incompressible (a restic pack fed to
`zstd -19` came back 1.00x; the plain source text it was made from compresses
4.20x). So the comparison that decides quota is on-disk against on-disk:

| | logical | on disk at the remote |
|---|---|---|
| plain, as today | 266.59 GiB | ~140 GiB (1.9x filesystem compression) |
| per-box restic repos | 68.635 GiB | ~68.6 GiB (ciphertext, incompressible) |
| **saving** | **3.9x** | **~2.0x** |

Both numbers are real and they answer different questions: 3.9x is transfer
volume, ~2.0x is the 5 TB quota. Neither is the primary argument, which is
transaction cost.

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

Five normalisation routes were tested. Four are dead: `backup --set-path` does
not exist; `rewrite` changes host and time but not paths; `cd <box> && backup .`
still records the absolute path; a bind mount would work and needs root.

**The fifth works, and it is what the implementation does.** A symlink as the
FINAL path component records the fixed path but archives *the symlink itself* —
a 0-file snapshot, silently. A symlink as an **intermediate** component is
recorded unresolved AND traversed:

| argument | recorded path | files archived |
|---|---|---|
| a real directory | its own path | all of them |
| a symlink to the box | the symlink's path | **0 — it archives the link** |
| `<symlink to the parent>/<box>` | the unresolved path | all of them |

So every machine backs a box up through
`/tmp/boxyard-restic/<index_name>/<index_name>`: the first component is a
symlink to that machine's checkout root, the second is the box's own directory.
Every machine records the identical string, and `--parent` and `restic diff` —
both of which match by path — work across differing checkout roots. Verified on
macOS too, where `/tmp` is itself a symlink to `private/tmp` and the path is
still recorded verbatim.

Two conditions come with it, both measured:

- **`--ignore-inode` is required.** Two machines never share inodes and restic
  compares them by default, so a replica that obtained its copy by restoring
  reports every file `changed` — `changed=2 unmodified=0` against
  `changed=0 unmodified=2` with the flag, while a genuine edit is still seen.
- **The canonical root must be validated, not merely created.** `/tmp` is
  world-writable and sticky everywhere here (`drwxrwxrwt` on mymain, ideapad and
  macOS's `/private/tmp`), so it is created `0700` and refused unless it is a
  real directory, not a symlink, owned by this user, and not group- or
  world-accessible. Otherwise a hostile or stale entry could redirect a backup —
  or a `restore --target` — outside the box.

Index names are unique, so two boxes never collide on one canonical name, and
the link is re-pointed **atomically before every use** (0.033 ms) rather than
created-if-missing: a crashed run can leave a link aimed at a checkout root the
box has since left, and silently backing up the old location would be worse than
any failure.

**Every machine that runs boxyard can do this.** They are all macOS or Linux and
all have a usable `/tmp`.

termux looked like a counter-example and is not one: it runs as an untrusted_app
uid and can write to no fixed absolute path (`/tmp` is mode 0771 owned by
`shell`; `/var/tmp` and `/data/local/tmp` are refused; its writable areas are
under `/data/data/com.termux/files/`, which Linux and macOS cannot create without
root). But **termux does not run boxyard at all** — verified: no `boxyard`
binary, no `~/.boxyard`, and the three `~/dev` entries are plain git repos with
no registration. myrig sets `no_pyinfra = true` for termux and boxyard is
installed by a pyinfra step, so it has never been installed there. What is left
is rendered leftovers, being removed separately.

So the canonical path is not a best-effort optimisation with a permanent hole in
it. It is the normal path for the whole live fleet.

The restore fix, verified, and still needed — the canonical path makes the
source a constant, but a box pushed before conversion, or from a machine that
cannot use the canonical root, still records its own:

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
- **Change detection stops being a mtime walk — for every machine, but only
  because of the canonical path.** `backup --dry-run --parent` reuses restic's
  own detection instead of `check_last_time_modified`.

  > **What this does and does not guarantee.** `backup --parent` matches the
  > parent's tree BY PATH, exactly as `restic diff` does. Measured:
  > byte-identical content under a different path reports
  > `files_new=1, files_unmodified=0`, against `files_new=0, files_unmodified=1`
  > under the same path. So restic's exact detection is available **only where
  > the snapshot was taken through the same path** — which is what the canonical
  > path (§2) buys, and why it is not an optimisation but a correctness
  > mechanism. Without it every Mac↔Linux replica would report CONFLICT forever.
  >
  > **In the live fleet that means exact detection everywhere.** Every machine
  > that runs boxyard is macOS or Linux and can use the canonical root.
  >
  > `parent_is_usable` is the fallback, and it stays, because two distinct
  > things reach it and only one of them expires:
  >
  > 1. **The parent snapshot no longer exists.** Permanent, and routine once
  >    retention ships: any machine's `forget` can remove the snapshot another
  >    machine's state record names. This is not a migration artefact and never
  >    goes away, so there is deliberately no `TODO(cleanup)` here.
  > 2. **The parent's recorded source path is not the one we are about to back
  >    up through.** Two sub-cases: snapshots taken before the canonical root
  >    existed, which expires when conversion completes; and the canonical root
  >    being unusable at runtime — a `/tmp` that is a symlink, world-writable, or
  >    owned by someone else, a machine we have not met, a platform we have not
  >    met. That half is defence in depth and also does not expire.
  >
  > When it fires, detection falls back to the mtime-versus-record-time test the
  > plain backend already applies in `get_sync_status` — which is why the local
  > state record carries `synced_at_unix`.
  >
  > So the honest statement is: **exact across the live fleet; today's mtime
  > semantics in the two cases above; and never a wrong answer in either.** The
  > failure direction is always "do more work", never "skip".

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

**Roughly 840 GiB of logical data exists only on the remote** (1,107.0 GiB total
minus mymain's 266.6 GiB checkout, modulo what the Macs hold; ~450 GiB of it on
disk after the remote's ~1.9x compression), and there is no server-side shortcut: the
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
3. **delete `boxyard/sync_records/<box>/data.rec`**
4. purge `boxes/<box>/data/`
5. write `boxes/<box>/data.snapshot`; set `storage_format` in `boxmeta.toml`

> **Correction to an earlier revision of this note, found while implementing it.**
> Steps 3 and 4 were originally the other way round — purge the tree, then delete
> the record. That leaves a real window. With `data/` gone but `data.rec` still
> present, `get_sync_status` finds matching sync records and reports
> `NEEDS_PUSH` for a box with local changes, so an un-upgraded machine
> **resurrects the plain tree beside the repository** and the two diverge with
> nothing reporting it.
>
> Deleting the record FIRST closes it, because `get_sync_status` opens with
>
> ```python
> if remote_path_exists and remote_sync_record is None:  # -> ERROR
> ```
>
> which fires the moment the record is gone, while `data/` is still there. With
> this order **every intermediate state is a loud refusal on every machine**, and
> there is no window at all. The interruption table below is tested row by row.
>
> A related detail that is easy to get backwards: the converting machine's own
> LOCAL `data.rec` is deliberately **not** removed during the conversion. Its
> presence is what makes the interrupted states report `ERROR` instead of looking
> like a fresh box that wants pushing.

| # | after | remote holds | this machine | an un-upgraded peer | recovery |
|---|---|---|---|---|---|
| 0 | nothing | `data/`, `.rec` | plain, syncs | plain, syncs | start again |
| 1 | repo pushed | + `data.restic/` | plain, syncs | plain, syncs; cannot see the repo | re-push is a cheap no-op |
| 2 | verified | unchanged | as 1 | as 1 | as 1 |
| 3 | `.rec` deleted | `data/`, repo | **ERROR** | **ERROR** | re-run continues |
| 4 | `data/` purged | repo | **ERROR** | **ERROR** | re-run continues |
| 5 | pointer written | + `data.snapshot` | ERROR until boxmeta | **ERROR** | re-run completes |
| 6 | boxmeta saved | complete | restic, syncs | **ERROR** | done |

**What the verification compares:** content, mode and symlink targets, plus the
set of paths in both directions. Not bytes alone — the claim being verified is
that restic carries mode and symlinks natively, and a check that only compared
content would not be checking the claim. The exec-bit manifest is excluded from
the snapshot on purpose, so it is excluded from the comparison too.

**What conversion refuses**, always before anything is written: a box being
synced right now (detected by taking the same per-box lock `sync_box` holds),
a box with an interrupted sync record on either side (no process holds the lock,
but the tree is not settled, so "byte-identical" would be verifying a torn tree),
a box not checked out on this machine (nothing to verify against), and a
`local` storage location (no remote, nothing to convert).

**`--dry-run`** reports the plan, the local file count and byte total, and
changes nothing. **`--dry-run --estimate-size`** additionally measures what
restic would store, by ingesting into a LOCAL temporary repository —
`data_added_packed`, the real packed size after deduplication and compression,
rather than a ratio borrowed from another box. It writes nothing to the remote.

The old procedure's failure mode, kept because it is why the order is what it is:

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

### The rollout constraint: every machine first, and that is slow

**A converted box is unusable on a machine still running an older boxyard.** The
gate above makes that a loud refusal rather than corruption, which is the point —
but it means **conversion must not begin until every machine has the new
boxyard.** Not "most", and not "the ones that matter": a single stale machine
turns every converted box into a permanent error there, and if it is a machine
that holds local work, that work stops leaving it.

This is a slow condition, not a checkbox. **macbook and pocket4 go offline for
days at a time** — both were unreachable twice during one afternoon of
measurement. So the fleet reaches a version when the last laptop is next opened,
which is a date nobody controls.

How a person should confirm it, before converting anything:

```bash
for m in mymain macbook macstudio ideapad pocket4; do
  printf '%-10s ' "$m"
  ssh-target "$m" 'boxyard --version' 2>/dev/null || echo UNREACHABLE
done
```

`UNREACHABLE` is not a pass. A machine that cannot be checked is a machine that
might be stale, and the honest reading is "not yet". termux is deliberately not
in that list: it does not run boxyard at all (verified — no binary, no
`~/.boxyard`, and its `~/dev` entries are plain git repos).

There is no automatic enforcement of this and deliberately so. A version
handshake would need a fleet-wide heartbeat mechanism that does not exist, and
inventing one to gate a one-off migration would be a permanent cost for a
temporary problem. The gate that DOES exist — every intermediate and final state
being a loud refusal — is what makes a mistake here recoverable rather than
destructive.

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
- **A THIRD schedule appears, and the cadence work is the right home for it.**
  Retention needs its own low-frequency pass because `forget` and `prune` both
  take exclusive locks (§10.1c). It reuses `due_boxes` unchanged by adding a
  third schedulable part alongside DATA and META — same check records, same
  most-overdue-first ordering, same zero-remote-call decision. See §10.2.
- **The 10-hour box stops holding the fleet hostage.** That wrinkle is listed as
  "known, non-architectural" in the cadence note; this removes its cause.
- **`compress` staying removed is confirmed correct.** Compression is a backend
  property: measured 1.76x on jackfruit on top of 3.4x deduplication, with no
  knob and no per-box decision.

---

## 9. What becomes redundant

Two pieces of existing machinery exist only because a plain `rclone sync` over
SFTP cannot do something. restic can. Both must be handled explicitly, because
leaving them running under restic is not neutral — one of them actively churns
the thing the whole design exists to stop churning.

### 9.1 The exec-bit manifest — skipped for restic-backed boxes

`.boxyard-perms.json` and `_utils/perms.py` exist for one reason: SFTP drops the
Unix mode bit, so boxyard records which files are executable before a push and
reapplies it after a pull.

**restic carries mode natively**, and better than the manifest does. Verified
through the exact restore form this design uses
(`restore <snap>:<source> --target`):

| file | source mode | restored mode |
|---|---|---|
| `run.sh` | 755 | **755** |
| `notes.txt` | 644 | **644** |
| `sub/tool.sh` | 775 | **775** |
| `link.txt` | symlink | symlink |

Note 775 survives exactly. The manifest cannot do that: it stores a boolean per
path and reconstructs the mode by mirroring read bits into exec bits, and its v1
is deliberately **additive-only** — it restores `+x` and never clears it. restic
restores the literal mode, including clearing. So this is a capability upgrade,
not just a removal.

**Decision:**

- `preserve_exec_perms` is **skipped entirely** for restic-backed DATA. No
  `generate_exec_manifest` before push, no `apply_exec_manifest` after pull.
- `_utils/perms.py` **stays, unchanged, for plain boxes.** SFTP has not changed
  and 594 boxes are plain today. It becomes plain-only, not dead.
- The push **excludes** the manifest from the snapshot, so a box converted while
  still holding one does not carry it forward:
  `--exclude <data_path>/.boxyard-perms.json`.
  Excluding rather than deleting keeps conversion non-destructive; the file is
  derived, so the first full restore with `--delete` cleans it up locally, and a
  box that ever reverted to `plain` would simply regenerate it.

> **Trap — the exclude and include anchoring rules are MIRROR IMAGES, and getting
> it wrong fails silently.** Measured, with a manifest at the DATA root and
> another in a subdirectory:
>
> | `backup --exclude …` | result |
> |---|---|
> | `.boxyard-perms.json` | excludes **both** — unanchored basename match |
> | `/.boxyard-perms.json` | excludes **neither** — anchors at the filesystem root |
> | `<abs data path>/.boxyard-perms.json` | excludes **exactly** the root one ✔ |
>
> So `backup` patterns match the **absolute** path, while `restore --include`
> under the `<snap>:<subpath>` form anchors with a **leading slash relative to
> the subpath** (§2). Same-looking flags, opposite rules.

### 9.2 `sync_backups/` — stops being written, and is already 116.4 GiB of residue

`sync_helper` passes `--backup-dir` to every `rclone sync` so an overwrite is
recoverable, then purges the directory on success (`delete_backup=True`).

For restic-backed DATA this **stops by construction**: the DATA path no longer
calls `rclone sync`, so no backup directory is created for it. No new mechanism,
nothing to switch off. **META and CONF keep theirs**, because they stay plain and
still need it. Restic snapshots are a strictly better answer for DATA anyway —
they are history with a retention policy rather than an unbounded side-pile.

**What about the existing 116.4 GiB?** Measured server-side, and it is not what
it looks like:

| | |
|---|---|
| total | **116.4 GiB logical** (38.6 GiB on disk) across **1,186 backup directories** |
| non-trivial (> 4 KiB) | 823 |
| oldest | 2025-11 |
| largest single directory | 4.52 GiB on disk |
| by month | 2025-12: 9.9 GiB · 2026-04: 8.1 GiB · 2026-05: 11.7 GiB · 2026-07: 0.01 GiB · 2026-08: 0.73 GiB |

Because `sync_helper` purges on success, **every one of these is the residue of a
sync that did not complete.** Cross-checking mymain's 783 local sync records
finds exactly **one** incomplete record — jackfruit, the sync that was in flight
while this was measured — and its ULID matches exactly one of the 1,188
directories. The other ~1,187 are orphaned.

The month profile also says the leak has largely stopped: 2026-04/05 contributed
19.8 GiB between them, 2026-07/08 contributed 0.74 GiB. That is consistent with
the v0.4.x–v0.5.x reliability work. What nothing ever did was clean up the
backlog.

**So the honest answer is that this is a pre-existing cleanup task that restic
neither causes nor fixes**, and it should be its own ticket rather than smuggled
into a storage change. Conversion stops the DATA half of the *source*; it
reclaims nothing.

A safe cleanup pass, if one is written:

- report before deleting, oldest first, with sizes; a dry-run manifest that a
  person approves, then delete strictly by explicit approved path;
- only consider directories older than a threshold (30 days is far beyond any
  in-flight sync), because a backup referenced by a live incomplete sync is the
  one case where it is not residue;
- check **every machine's** incomplete records, not just the local ones. Only
  mymain's were examined here; macbook, macstudio, ideapad and pocket4 each keep
  their own, and pocket4 is offline for days at a time.

**Costed, because 50,802 objects sounds expensive and is not.** Measured by
uploading 500 small objects to a scoped probe path and timing their removal:

| | rate | 50,802 objects |
|---|---|---|
| `rclone purge`, default concurrency | **10 ms/object** | **~8.5 min** |
| `rclone purge --transfers 16 --checkers 16` | 13 ms/object | ~11 min |
| *(upload, for contrast)* | 75 ms/object | — |

Two things to take from this. The single-path probe figure of 0.67 s is
**connection setup**, not per-operation cost — inside one rclone invocation the
connection is reused and deletes run ~65x faster, so costing the cleanup at
0.67 s/object would predict 9.5 hours for a job that takes about ten minutes.
And raising concurrency does not help and slightly hurts, so the sane shape is
**one `rclone delete --files-from <approved paths>` (or a purge per approved
directory) at default concurrency**, not a fan-out and not a per-object loop.

**The LOCAL `~/.boxyard/sync_backups` is a separate, trivial matter** — 20 KB
across 4 directories on mymain. The remote figure implies nothing about it. (Its
incomplete-record count is 0 or 1 depending on whether a sync is in flight at the
moment you look; it was 1 during this measurement, for the jackfruit push that
was running.)

**This is a different problem from the stranded objects** the exclude extension
left behind (files already pushed under a name that is now excluded, which go
invisible and stay forever). Same directory tree, unrelated cause, unrelated fix.
Do not merge the two.

---

## 10. Point-in-time recovery, and the retention decision

Worth naming because it is a capability boxyard has never had, and because its
flip side is a decision that must be made **before** this ships.

restic keeps a snapshot **timeline**, not a current state. `restic snapshots`
lists every snapshot with ID, time, host and tags; a box's DATA at any past
snapshot can be restored to a scratch directory with the same
`restore <snap>:<source> --target` this design already uses. So "what did this
box look like three weeks ago" becomes answerable for boxes that are not git
repos — notes, datasets, scraped corpora, design assets. That is most of the
yard's bytes.

**Not building it in this scope.** It costs a command over machinery that
already exists, and it should wait until the storage change itself is settled.
Naming it because the build order should not look like it was overlooked.

### The retention decision, which cannot wait

**With no `forget` policy, a restic repo keeps every snapshot forever.** That is
a real decision with a real cost, and it needs an answer before conversion
starts, not after. "How far back should a box's history be kept" is a policy
question, and it belongs on the **existing** `_sync_policy` axis — a third
dimension beside `data_interval` and `meta_interval`, resolved by the same rule
(box `conf/sync.toml` → matched group policy → default), never a second
mechanism:

```toml
[sync_policies.default]
data_interval = "6h"
retention     = "90d"

[sync_policies.cold]
groups        = ["archived", "dormant"]
data_interval = "7d"
retention     = "365d"
```

**First line of defence is structural, and is already in the design:** a box
whose status resolves to `SYNCED` is never backed up at all, so snapshots
accumulate with *actual changes*, not with passes. A no-op snapshot would cost
**348 B** (287 B stored) — measured — but the design takes none. This alone
bounds a quiet box at zero growth, which is most of the yard.

The sections below critique the agreed design against measurement (10.1), settle
the maintenance pass and cost it (10.2), work through the cadence interaction
(10.3), and answer the correctness question that retention creates (10.4).
**Retention is designed here and built last** — after `storage_format` and
`boxyard convert` — but it is designed *now* because it and the pointer design
are the same problem.

### 10.1 Critique of the agreed design — three things the measurements change

The agreed shape (retention as a policy dimension, `forget` split from `prune`,
a weekly whole-yard `boxyard maintain` on mymain) is right, and most of it is
confirmed below. Three parts do not survive measurement.

#### (a) The stated justification is wrong. The conclusion is still right.

> *"Every snapshot is a FILE in the repo's `snapshots/` directory. The cheap
> keyless skip-check works by listing that directory for every box in one call.
> So unbounded snapshot growth directly attacks the property that made per-box
> repos viable."*

It does not, for two independent reasons.

**The skip check does not list `snapshots/`.** It reads
`boxes/<box>/data.snapshot` — one file per box at depth 2, the same size
whether the repo holds 1 snapshot or 100,000 — out of the bulk listing that
`--skip-unchanged-meta` already runs (§2). Listing `snapshots/` per box was
considered and rejected in favour of the pointer precisely because it costs a
call per box. Snapshot count cannot touch it.

**And snapshot count does not degrade repo operations anyway.** Measured on one
repo grown in place, everything else held constant:

| snapshots | `backup` | `snapshots` | `forget --dry-run` | `snapshots/` on disk |
|---|---|---|---|---|
| 0 | 0.86 s | 0.78 s | 0.82 s | 450 B |
| 100 | 0.80 s | 0.79 s | 0.78 s | 50 KB |
| 500 | 0.83 s | 0.83 s | 0.82 s | 248 KB |
| 823 | — | — | 0.90 s | ~410 KB |

Flat. All three are dominated by the ~780 ms key derivation; the snapshot list
is noise beside it. Even 2,160 snapshots would be about 1 MB in one directory.

This matters because the wrong reason licenses the wrong remedy: if snapshot
growth were attacking the skip filter, `forget` would have to run continuously
to defend it, which is the argument for putting it in the sync loop. It is not,
so the case for retention rests entirely on **storage** — which is sufficient on
its own, and points at a periodic pass rather than a hot-path one.

#### (b) The proposed value expansion does not thin. Nor did mine.

> *"`retention = "90d"` … expanding to `--keep-within 7d --keep-daily 90
> --keep-last 10` — full granularity for a week, daily beyond, with a floor.
> ~110 snapshots rather than 2,160."*

The diagnosis — *retention must THIN, not merely expire* — is exactly right. The
expansion does not do it. Measured against a repo of **823 snapshots all created
the same day**, which is what an actively-edited box looks like, and what *every*
box looks like if the DATA cadence is shortened after migration (§8):

| policy | keeps | removes |
|---|---|---|
| `--keep-within 7d --keep-daily 90 --keep-last 10` *(proposed)* | **823** | **0** |
| `--keep-within 90d` *(what I recommended in the previous draft)* | **823** | **0** |
| `--keep-last 10 --keep-hourly 24 --keep-daily 30 --keep-weekly 13 --keep-monthly 3` | **11** | 812 |
| the ladder **plus** `--keep-within 1d` as a "floor" | **823** | **0** |

Three things follow, and the last is the trap.

1. **`--keep-within` is an EXPIRY rule, not a thinning rule.** It keeps
   *everything* newer than its window. Leading the expansion with
   `--keep-within 7d` re-admits the whole week unthinned, which is the exact
   problem the expansion was written to solve.
2. **My own previous recommendation was wrong the same way**, and worse: a bare
   `--keep-within 90d` thins nothing for 90 days. I had checked that it is
   measured relative to the newest snapshot (so a dormant box keeps its history
   — that part stands) and did not check that it thins. It does not. Withdrawn.
3. **restic's policy is a UNION, so adding a `--keep-within` floor to a working
   ladder destroys it** — 11 keeps becomes 823. "Protect the last day from
   thinning" is not expressible as an addition; the protection of recent work is
   `--keep-hourly`, which is bounded, and `--keep-last N`, which is a floor in
   count rather than time.

**Corrected sugar.** `retention = "<N>d"` still reads as "how far back history is
kept", which is the question actually being asked, but it must expand to a pure
`--keep-N` ladder:

```
retention = "90d"   ->  --keep-last 10 --keep-hourly 24 --keep-daily 30
                        --keep-weekly 13 --keep-monthly 3
retention = "365d"  ->  --keep-last 10 --keep-hourly 24 --keep-daily 30
                        --keep-weekly 52 --keep-monthly 12
```

i.e. `--keep-last 10 --keep-hourly 24 --keep-daily min(N,30) --keep-weekly
ceil(N/7) --keep-monthly ceil(N/30)`. Bounded at roughly 80 snapshots for `90d`
and 130 for `365d` **regardless of how often the box changes**, which is the
property the scalar has to have if it is going to be the common case. The
explicit table form stays available for the rare box that wants something else:

```toml
[sync_policies.default.retention]
hourly = 24
daily  = 30
weekly = 13
```

`--keep-last 1` is implied whatever the policy says, so a box can never lose its
only snapshot to a config typo.

#### (c) `forget` in the sync loop: viable, but it roughly doubles push cost

> *"`forget` runs in the SYNC loop, on every machine, after a successful push.
> It only deletes small snapshot files. Cheap."*

**First, a correction to my own previous draft.** I said `forget` *cannot* run in
the sync loop because it takes an exclusive lock. The lock is real —

```
unable to create lock in backend: repository is already locked exclusively
  by PID 3203767 on mymain by lukastk
```

— but "cannot" was too strong, for two reasons I had not weighed. With **per-box**
repos the contention is only between machines touching the *same* box, and the
cadence note measures only 7 of 278 boxes (3%) as existing on more than one
machine. And `--retry-lock` converts the collision from a failure into a wait:
measured, a `backup --retry-lock 30s` colliding with a `forget` succeeded in 3
trials of 3, waiting 5.83 / 5.88 / 5.85 s. So the lock is a manageable hazard,
not a disqualifier — **provided every restic invocation passes `--retry-lock`**,
which the design should mandate regardless.

**The real objection is cost, and it is measured over the actual remote:**

| operation, per box, over SFTP | time |
|---|---|
| repo open (`cat config`) | 2.02 s |
| `forget --dry-run` | 2.18 s |
| `forget` (real, removes snapshots) | 2.87 s |
| `prune --max-unused 10%` (repacking) | 4.94 s |
| `prune --max-unused 10%` (nothing to do) | 3.73 s |
| *(for comparison)* no-op push | **3.1 s** |
| *(for comparison)* first push | **4.6 s** |

A `forget` after every push costs **2.87 s on top of a 3.1–4.6 s push** — it
close to doubles the cost of the operation this entire design exists to make
cheap. And by (a) it buys nothing that needed defending.

**So: `forget` joins `prune` in the weekly pass, and neither is in the sync
loop.** Not because `forget` is unsafe there — it is survivable with
`--retry-lock` — but because it is not cheap there and there is no longer a
reason to pay.

The middle ground, if continuous thinning is ever wanted: run `forget` only when
the push actually *created* a snapshot, never after a no-op. Real pushes are rare
per box, so the amortised cost is small. It is a strictly optional refinement,
and it should not be built first.

### 10.2 `boxyard maintain` — weekly, on mymain, over the whole yard

**Agreed, and the three reasons hold.** Maintenance acts on the repository, not a
working tree, so inclusion is irrelevant and mymain servicing a box it has no
copy of is the normal case — which dissolves the "who prunes, given 321 unowned
boxes" problem rather than solving it. One writer means no contention by
construction. And mymain is always on, wired and unmetered, where repacking
belongs; a `prune` on macbook over wifi or termux on mobile data is exactly what
this avoids.

**The conf-availability measurement is confirmed**, independently, on the live
remote and locally:

| | claimed | measured |
|---|---|---|
| bulk `lsjson` over `boxes/*/conf/**` | 9.2 s | **9.3 s** |
| boxes with any conf file on the remote | 5 of 594 | **5 of 594** |
| local conf dirs on mymain | 68, of which 5 non-empty | **68, of which 5 non-empty** |

The five are all `.rclone_exclude`. So resolving per-box overrides from mymain is
one bulk listing plus a filtered copy of five files — the pattern
`sync-missing-meta` already uses, not 594 round trips. And the consequence is
worth stating in the design: **retention will in practice be a GROUP-level
setting for essentially every box**, with `conf/sync.toml` as a rare escape
hatch. That is the right shape, and it is what the per-dimension resolution in
`resolve_policy` already gives.

**Cost of the weekly pass**, from the measured per-box figures above:

| pass | serial | at concurrency 8 |
|---|---|---|
| `forget` over all 594 boxes | ~28 min | ~3.5 min |
| `prune` over all 594 boxes (mostly no-ops) | ~37 min | ~4.6 min |

Both are affordable weekly on an always-on machine, and key derivation
parallelises well enough to make the concurrency real (§1).

**One correction to the operational plan:** *"`prune --max-unused` with a
threshold, so most weeks are a listing and nothing else"* — a `prune` with
nothing to do still costs **3.73 s per box**, because it still opens the repo and
loads the index. `--max-unused` avoids the *repacking*, not the visit. Over 594
boxes that is 37 minutes of doing nothing.

The fix is a **local** gate rather than a remote one: only `prune` a box whose
`forget` in this same run actually removed snapshots. `forget` reports that, it
costs nothing extra to know, and it turns `prune` from 594 visits into the
handful of boxes that changed. That is the difference between a weekly pass that
is mostly free and one that takes half an hour to discover it had no work.

**Rate-limiting and resumability come free from the cadence work**, as proposed:
`_sync_policy.due_boxes(config, metas, part, now)` is already parameterised by
part and already decides across 590 boxes in ~171 ms with zero remote calls.
Adding a third schedulable part — `MAINTENANCE` — gives due-ness, most-overdue-
first ordering, and degrade-to-due-now on a missing record, with no new
mechanism. A run that cannot finish stops at N boxes and the rest are simply
still due next week. Suggested `retention_interval = "7d"`, and the pass should
carry a wall-clock budget so it can never overlap into a sync window.

Not a threshold on reclaimable bytes as the trigger: computing that requires
opening every repo, which is 594 × 2.02 s to decide whether to do any work at
all. An interval is free local state.

### 10.3 Retention interacts with cadence — and the interaction has a sharp edge

Snapshot count is a function of how often a box actually *changes*, not of how
often it is checked, because a `SYNCED` box is never backed up at all (§4). So:

| policy | `data_interval` | max snapshots/day | `retention` | retained |
|---|---|---|---|---|
| `default` | 6h | 4 | `90d` | ~80 |
| `cold` | 7d | ~0.14 | `365d` | ~52 |

With the corrected ladder these are bounded by the *ladder*, not by the cadence,
which is the point of fixing (b): the same config means the same thing at both
ends.

**The sharp edge:** §8 recommends shortening the DATA cadence back toward META's
once boxes are converted, because a no-op DATA check stops being expensive. Under
the *proposed* `--keep-within 7d` expansion that would have been actively
harmful — a 15-minute cadence puts up to 672 snapshots inside the unthinned
window. Under the ladder it is a non-event. So fixing the retention shape is a
precondition for the cadence change, not an independent decision.

What is never bounded by any of this is retained **content**: every version of
every changed file is kept. Nothing for text; a lot for `tbi-investigation`
(95.2 GiB) or `aisi-economy-index-v2` (178.8 GiB), where one regeneration costs
the full unique size again. That is the real reason retention exists, and it is
why `cold` — archived boxes that barely change — can afford a long window
cheaply while an active data box cannot.

### 10.4 The offline-machine correctness question — answered, and tested

*A machine offline for weeks returns to a repo whose snapshots another machine
has forgotten, holding a pointer to a snapshot that no longer exists.*

This was the right thing to worry about, and **the first version of the prototype
got it wrong, in the dangerous direction.**

Two measured facts, which disagree with each other:

| command, with a snapshot that no longer exists | behaviour |
|---|---|
| `restic snapshots --json <gone>` | **rc=0**, stdout `[]`, warning on stderr |
| `restic backup --parent <gone>` | **rc=1**, no summary emitted |

A returncode check misses the first entirely and reads the second as a general
failure. The prototype's `local_is_modified` did exactly that — `rc != 0` →
"assume changed" — so a clean replica that had merely been offline reported
**CONFLICT**: no data lost, but the box stops syncing on that machine until a
human intervenes. With pocket4 dark for days and most boxes unowned, that is
routine, on the machine least able to notice.

**The fix: the local state record carries `synced_at_unix` beside the snapshot
ID.** When the recorded snapshot is gone, modification is decided by the
timestamp — the same mtime-versus-record-time test the plain backend already
applies in `get_sync_status`. Not a new mechanism, and not a weaker guarantee
than today's.

Behaviour now, all four cases tested end to end (e2e checks 9):

| | verdict | action |
|---|---|---|
| base forgotten, replica **unmodified** | `NEEDS_PULL` | full restore (`full-base-forgotten`); converges byte-identically |
| base forgotten, replica **edited** | `CONFLICT` | nothing touched; local work still on disk |
| base present, remote moved | `NEEDS_PULL` | diff + targeted restore |
| base present, both moved | `CONFLICT` | nothing touched |

It degrades for a second, independent reason too: `pull` asks the base snapshot
for its source path, gets `None`, and takes the full-restore branch. So even if
the status layer were wrong, the transfer layer would not act on a bad diff.

**It can never conclude "nothing changed"** — that verdict requires the remote
pointer and the local record to be *equal*, and a forgotten base cannot make them
equal. The failure direction is always "do more work", never "skip".

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
   plain path stays exactly as it is. This is also where `preserve_exec_perms`
   becomes plain-only (§9.1) — one branch, not a second mechanism.
5. **`--skip-unchanged` extending the existing bulk listing.**
6. **`retention` + `retention_interval` as policy dimensions, plus a
   `boxyard maintain` that runs `forget`/`prune` deliberately** (§10). This has
   to exist before the first box is converted, or repos start growing with no
   ceiling and no command to give them one. The scheduling half is a third
   schedulable part in `due_boxes` — no new mechanism.
7. Only then: `restic` as the resolved default for new boxes.

**Deliberately not building:**

- **A shared repo per storage location.** Measured at 7% of stored bytes, for
  keyless skip-filtering, per-box independence, cheap deletion and a
  proportionate index.
- **Restic-backing META or CONF.** `sync-missing-meta`'s one bulk `lsjson` over
  `boxes/*/boxmeta.toml` at depth 2 is what box discovery rests on. Non-negotiable
  without a replacement discovery mechanism, and there is no reason to want one.
- **Automatic conversion of existing boxes**, in either direction.
- **`prune` OR `forget` in the sync loop.** `forget` there is *survivable* with
  `--retry-lock` (§10.1c corrects an earlier over-claim), but it costs 2.87 s per
  box against a 3.1 s no-op push — it roughly doubles the operation this design
  exists to make cheap, and by §10.1a it defends nothing. Both go in the weekly
  pass.
- **Point-in-time recovery as a command.** It becomes possible (§10) and it is
  worth having; it is not this change.
- **Reclaiming the 116.4 GiB of `sync_backups` residue.** Real, and a separate
  ticket: restic neither causes it nor fixes it (§9.2).
- **Removing `_utils/perms.py`.** It stays for plain boxes; SFTP has not changed
  (§9.1).
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
ssh -p 23 u508472@… "du --inodes --max-depth=1 ./boxyard/boxes"
ssh -p 23 u508472@… "du -k --max-depth=1 --apparent-size ./boxyard/boxes"
```

Server-side, no per-file network round trips, minutes rather than the hours an
`rclone` recursive listing of 1.4M objects would take. This is how every yard-
scale number here was obtained.

> **`--apparent-size` is mandatory, and leaving it off is a 2-3x error.** This
> note's first revision quoted plain `du`, which reports *allocated blocks*, and
> presented it as logical size. The storage box compresses transparently, so the
> two differ badly:
>
> | | plain `du` (on disk) | `du --apparent-size` (logical) | ratio |
> |---|---|---|---|
> | `boxyard/sync_backups` | 38.55 GiB | **116.40 GiB** | 3.02x |
> | `boxyard/boxes` | 592.8 GiB | **1,107.0 GiB** | 1.87x |
> | whole `boxyard` store | 631.4 GiB | **1,223.4 GiB** | 1.94x |
>
> `du --apparent-size` on `sync_backups` returns 116.40 GiB against an
> independent `rclone size` of 116.396 GiB — they agree, which is what confirms
> the method rather than the tool. `du --count-links` returns the same 38.55 GiB,
> so hardlink de-duplication is ruled out and compression is the cause.
>
> Note the error is not even in a consistent direction: on a tree of many tiny
> files, block rounding pushes plain `du` the *other* way (measured 1.05-2.47x
> too HIGH on three small boxes). So a plain-`du` figure cannot be corrected
> after the fact by a fudge factor; it has to be re-measured.
>
> Both numbers have legitimate uses — on-disk is what consumes the 5 TB quota,
> logical is what has to cross the wire — but they must be labelled. `df`
> reporting 3.3 TiB used is an on-disk figure.

Current shape of the yard, for reference:

| | |
|---|---|
| boxes | 593 |
| inodes (files + dirs) | 1,376,982 |
| bytes (logical) | 1,107.0 GiB |
| bytes (on disk) | 592.8 GiB |
| median box | 196 inodes, 3.3 MiB logical |
| p90 box | 3,034 inodes, 872 MiB logical |
| largest box | 280,061 inodes (`jackfruit-hq-mymain`) |
| skew | top 1 box = 20.3% of inodes; top 10 = 49.2%; top 50 = 81.9% |
| whole store | 1,223.4 GiB logical / 631.4 GiB on disk, including 116.4 GiB logical of `sync_backups` |
| filesystem compression | ~1.9x on box data, ~3.0x on backup residue |

> One correction to the ticket's premise while we are here. It cites
> jackfruit-hq-mymain as 687,876 files / 7.52 GiB. Measured today with the
> **current** default excludes it is **134,551 files / 3.760 GiB** locally, and
> 280,061 inodes on the remote. The exclude list was extended on 2026-08-28
> (myrig `44c7706`) with `.svelte-kit/`, `paraglide/`, `target/` and others,
> which cut the box roughly 5x before restic was involved at all. restic's win is
> still large — the baseline it is measured against has simply moved, and the
> remote still carries the stranded objects because an exclude never cleans the
> remote.

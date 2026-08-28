# Sync cadence and per-box sync policy — design note

Status: **approved in principle by Lukas 2026-08-28**, not yet implemented.
The `compress` half is still open pending two child threads (see *Open* below).

## The problem

Box META — names, groups, parents, `write_owner` — is what the machines use to
talk to each other. The cockpit pickers, the group symlinks, myvault notes and
the sesh boxyard plugin all read it. META latency is user-visible across
machines; DATA latency mostly is not.

Today META is synced **inside** the DATA pass, so it moves at the speed of the
slowest box in the yard.

Measured on mymain, 2026-08-27/28 (590 boxes):

| fact | value |
|---|---|
| `multi-sync` iterates *every* registered box, not just included ones | 590 `meta.rec` vs 120 `data.rec` |
| the fast 15-min loop is **discovery-only** | `missing_metas = sorted(_ls_remote - _ls_local)` |
| ...and is **pull-only** — no push anywhere in the command | only transfer is `source=<remote>, dest=""` |
| full pass duration | 37 min typical, **10 h** worst |
| effective META convergence | median 84 min, worst 10 h |

So one oversized box (`jackfruit-hq-mymain`, 241,066 files) holds the entire
fleet's catalog hostage. That is the bug.

## Decisions taken

1. **META syncs frequently; DATA syncs rarely.** META is the communication
   channel. (Lukas, 2026-08-27.)
2. **DATA cadence: 6h for non-archived boxes.** Note the binding constraint is
   *backup recency*, not conflicts — only 7 of 278 boxes (3%) exist on more than
   one machine, so concurrent DATA writers essentially do not happen here. 6h
   means up to 6h of work living on one disk; that is the trade being accepted.
3. **Schedule is per (box, PART).** Never a single per-box interval — a slow
   DATA cadence must not drag META with it.
4. **`type` and `schedule` are independent axes.** Do not bundle them.
5. **`null` is NOT a cold-policy group.** Cold = `archived`, `dormant` only.

## META sync: the "B-prime" skip filter

Rejected alternatives, and why:

- **A — run `boxyard multi-sync -c meta` on its own loop.** Works today with
  zero code (`--sync-choices/-c` exists; `sync_box` already skips CONF/DATA when
  only META is requested). But it costs 2 remote calls per box per pass = 1,180
  calls ~= **6.6 min** at `max_concurrent_rclone_ops=2`, ~7x more than needed.
  Kept as the zero-code stopgap if relief is wanted before this ships.
- **B — extend `sync-missing-meta` to refresh changed boxmetas.** The *pull*
  half is nearly free. But the command has no push direction at all, so this
  gives a machine a fast inbox and a slow outbox. Building the outbox means
  local-change detection, the push, sync-record updates both sides, and the
  both-sides-changed merge — i.e. **a second implementation of META sync**
  alongside `sync_box`'s, which must then stay in step forever. Rejected as
  exactly the tech-debt shape AGENTS.md warns about.

**Chosen: use the bulk listing as a skip filter in front of the existing
per-box path.** Reimplement nothing.

Each pass:

1. One bulk `rclone lsjson --recursive --fast-list --max-depth 2 --filter
   "+ boxmeta.toml"` gives `ModTime` + `Size` for every remote boxmeta.
   **This listing already happens today** — the current code fetches it and
   discards both fields, diffing only the path sets.
2. Stat the local boxmetas. Free.
3. A box where *both* sides match what was last agreed is **skipped with zero
   remote calls**. Everything else goes through the existing, tested `sync_box`
   META path, which already handles push, pull, merge and records correctly.

Typical pass: 1 listing + a handful of changed boxes x 2 calls ~= **1 minute**,
both directions.

Two safety properties:

- A wrong "changed" verdict merely does the correct sync anyway. Only a wrong
  "unchanged" could hide something.
- The DATA pass still performs a full, unfiltered META sync, so it is a backstop
  for anything the filter wrongly skips.

**Why a wrong "unchanged" cannot happen** — note this argument was WRONG in the
first version of this note and is corrected here. The original claim was that a
preserved `ModTime` "needs a remote write that preserved ModTime, which SFTP does
not do". That is false: rclone *does* set modtimes on this SFTP remote — the live
supervisor log carries `failed to set directory modtime` errors, which only arise
because it tries. Thread 462e4e6b caught this and confirmed it by round-tripping a
file stamped `2021-03-04T05:06:07Z` and reading the same value back.

The filter is still safe, for a different reason:

- `BoxMeta.save()` writes through `tmp_path.write_text(...)`, so an edited boxmeta
  always carries a fresh mtime — an edit cannot preserve the old one.
- `Size` is compared as well as `ModTime`.

So the only way to present an unchanged `(ModTime, Size)` pair with different
content is a byte-identical push, which is a no-op by definition. Nothing later
should lean on the discarded SFTP premise.

## Scheduling: no new architecture

`multi-sync` **already** accepts `--box/-r <index_name>...`, so "sync exactly
these boxes" is solved. What is missing is only *deciding which*.

One loop, waking every ~15 min:

1. `due_boxes(config, now)` -> `[index_name]` where
   `last_synced + interval <= now`. `last_synced` is already on disk in the sync
   records; `interval` comes from policy resolution. **Pure local state — zero
   remote calls to decide.**
2. `boxyard multi-sync --box <due...>`

No daemon, no per-box timer, no cron fan-out. The whole scheduler is one
function; the genuinely new work is the *policy model*, not the scheduling.

### Resource cost of the 15-minute loop — measured, not estimated

Lukas's explicit constraint: the loop must not consume too many resources.
Measured on mymain over all 590 boxes:

    stat 774 sync records            33.7 ms
    parse 590 meta.rec files        116.8 ms
    load boxyard_meta.json            4.2 ms
    stat 590 local boxmeta.toml      16.3 ms
                              TOTAL  171.0 ms

The decision layer is free. The only real cost is the bulk network listing —
which the existing loop already pays every 15 minutes. **B-prime does not add
load; it makes the listing already being fetched do useful work.** Keep the
existing `nice -n 19` / `ionice -c3` / slow-core `taskset` pinning from
`boxyard-meta-sync.sh`.

## Policy model

Named policies in `config.toml`; groups map to a policy name:

```toml
[sync_policy.default]
data_interval = "6h"
compress = false

[sync_policy.cold]
data_interval = "7d"
compress = true
groups = ["archived", "dormant"]   # NOT null -- see below
```

Per-box override lives in the box's own `conf/` — boxyard's established place
for per-box config, and it travels with the box:

```toml
# conf/sync.toml
data_interval = "1h"    # type inherited from the policy; the axes are independent
```

**Resolution: box `conf/` -> matched group policy -> default, per dimension.**
So a box can override only its cadence and keep the policy's compression.
Adding a box to `archived` then automatically gives it the cold cadence *and*
compression with no separate action — the desired behaviour.

Deliberately **not** stored in boxmeta: boxmeta syncs constantly and is the
thing that actually diverges, so putting schedule fields there enlarges the
divergence surface for no gain. `conf/` changes rarely.

### Why `null` is excluded, and what it buys

`null` does not mean "cold", and measured on the live yard it never appears
alone: 356 boxes are `archived`, 125 are `archived`+`null`, 23 are `dormant`,
86 are neither. So:

- **Zero boxes are `null` without `archived`/`dormant`** — dropping it changes
  nothing about which boxes are cold today.
- **Zero boxes match both `archived` and `dormant`** — with `null` gone the
  lifecycle groups are perfectly exclusive, so the group-conflict rule below has
  no findings to report.

### Group conflicts

527 of 590 boxes are in more than one group, so "the group's default" is
ambiguous in general. The rule is **not** a precedence list (a hand-maintained
global ordering that silently changes existing boxes whenever a group is added)
and **not** a most-conservative-wins join (no correct join direction for
`interval` — "shortest wins" defeats an archive schedule, "longest wins" means
any slow group silently slows a box).

The rule is: **a box may not match two groups naming different policies for the
same dimension**, reported by `boxyard doctor`, never silently joined. Because
sync policy is a function of *lifecycle* and not of topic, and the lifecycle
groups are exclusive, this fires on nothing today — and only ever fires when
genuinely contradictory policies are asked for.

## Known wrinkles (none architectural)

- A 10-hour box still blocks the loop while it runs. `jackfruit-hq-mymain` needs
  its own lane, or fixing — see the many-files ticket.
- A machine that was off returns with everything overdue. Sync most-overdue
  first; no catch-up storm, since it is one sync either way.

## `compress` is specified but REFUSED

The policy model has a `compress` field because the design refers to it. Nothing
implements packing, and `compress = true` is therefore **refused at config load**
by both implementations, naming the key and saying it is not implemented.

This is not caution for its own sake. Accepting the key would produce a config
that SAYS archived boxes are compressed while every archived box stays a plain
tree — discoverable only by going and looking at the remote. Of the three honest
options (implement it, remove the field, refuse the value), refusing is the one
that is true today. `TODO(cleanup)` in both trees, tied to packing being
implemented or the field being removed.

### If packing is ever implemented: the migration is the hard part

Asked by Lukas 2026-08-28, and it is the question that should decide whether to
build this at all.

- **Boxes checked out locally** (120 on mymain, 158 on macbook) are easy: the
  next push after the policy changes packs them.
- **Boxes that exist only on the remote are the problem.** 312 of 590 are on no
  machine. Converting one means downloading the whole tree to SOME machine and
  re-uploading it as an archive. There is no server-side conversion — rclone can
  move and rename remotely, but it cannot tar. For an archived box that is pure
  cost with no local benefit.

Three options, none free:

1. **Lazy** — a box becomes packed only when a machine holding it pushes. No
   migration, but the remote holds two formats indefinitely and every reader
   must handle both, forever.
2. **Eager** — a one-off pass pulling and repacking all 312. Correct, and the
   single most expensive operation this fleet would ever run.
3. **Never convert; pack new pushes only.** Honest, and the split is permanent.

A fourth hazard sits on top of all three: during any rollout a machine on an
older boxyard sees `data.tar.zst` where it expects `data/`. What it does then is
untested and potentially destructive.

**Assessment:** this strengthens thread 462e4e6b's "do not implement compress"
conclusion. The migration cost lands almost entirely on the boxes that benefit
least, and the measured alternative — excluding `.svelte-kit` and `paraglide`,
plus raising `RCLONE_TPSLIMIT` from 10 to 50 — turns the 10-hour box into a
~10-minute one with no format change at all.

## Open

- **`compress`** — whether packing is the right answer at all is still being
  worked by thread `462e4e6b` / ticket `0f3056f7`. The policy model has the field
  and does not depend on the answer.
- **The default exclude list** — extended 2026-08-28 in myrig `44c7706` with the
  unambiguous names. `build/ dist/ target/ vendor/ coverage/ deps/` are being
  surveyed by thread `f10a2245` / ticket `e599078f`. `.cache/` is permanently
  out: Lukas keeps cached API responses there.
- **Stranded remote files** — an exclude does NOT clean the remote (verified:
  rclone filters apply to both sides, so already-pushed files under a now-excluded
  name go invisible and stay forever). A one-time cleanup is part of the survey.

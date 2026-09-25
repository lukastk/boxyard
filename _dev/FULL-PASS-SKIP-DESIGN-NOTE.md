# Making a full `multi-sync` pass skippable — design note (v3)

Status: **implemented (unreleased), under review.** Written 2026-09-25; v2 the
same day after the first adversarial review (claude); v3 after the second (pi),
which changed the mechanism: the remote signal is no longer a timestamp stamp
but the record's own identity, published in the listing. **v4 during
implementation**, with three corrections the tests forced (marked "v4" below).
Everything measured here was measured on the live fleet that day.

---

## The problem, stated correctly

The supervisor loop on every machine runs `boxyard multi-sync --max-concurrent 2
--skip-unchanged` every 20 minutes over all 632 registered boxes (626 plain, 6
restic on mymain). Measured pass durations: mymain 44–50 min, macbook 39–63 min,
duty cycle 60–90%. On a day where nothing changed in any box — most days, for a
laptop — that is 600-odd remote round trips proving nothing.

`--skip-unchanged` skips nothing on a full pass, and says so on stderr every
pass (19 times in `boxyard-sync.err.log` on mymain): a box is skipped only when
**every requested part** is provably unchanged, META skipping is gated on its own
flag, CONF has no remote signal at all, and plain DATA (626 of 632 boxes) was
never skippable — `data_boxes_needing_sync` only knows restic's pointer file.

So the feature is: **every part of a box must be provable from bulk listings, so
a full pass can put an untouched box aside entirely.** Where it is not provable,
the box goes through the real sync path, exactly as today.

## Why the v1/v2 mechanism was wrong (both reviews, independently)

v1/v2 compared the remote record's `(ModTime, Size)` from a listing against a
stamp recorded at the last successful check. Two independent reviewers
reproduced a wrong skip in that mechanism — in the EXISTING META filter, today:

- **The stamp is taken from a listing at pass END.** A foreign push landing
  between a box's sync and that final listing is adopted as "what we agreed
  with"; the next pass skips a box whose remote moved. The window is the rest
  of the pass — minutes, i.e. a sizeable fraction of owner pushes at today's
  duty cycle. Harmless today only because a full pass never skips.
- **`(ModTime, Size)` is not a revision identity.** SFTP truncates mtimes to
  the second and rclone sets them from the PUSHER's clock; two records of the
  same part collide via a same-second push, clock skew, a backwards clock
  correction, or any restore that preserves mtimes; equal-length hostnames
  (macbook/ideapad/pocket4 are all 7) give equal Sizes. And doctor's
  `diverged-box` prefilter skips remote records within 5 s of the local ULID
  time — precisely the colliding window — so it is not the independent backstop
  v1 claimed.

## The mechanism (v3): the record publishes its identity

Every remote sync record write (`SyncRecord.rclone_save` with a non-local
destination) also writes a **zero-byte generation marker** beside it:

    sync_records/<box>/<part>.rec              (the record, as today)
    sync_records/<box>/<part>.rec.<ULID>       (the marker; ULID = the record's)

in the one order that is safe against a crash at any point (**v4**): sweep the
old `<part>.rec.*` markers, write the record, write the new marker. Every
prefix of that sequence leaves either the old state or NO marker, and no
marker reads as unknown. The v3 order (write the marker, then sweep) had a
window -- record written, marker not yet -- in which the old marker stood
beside a record it no longer described, and a machine holding that identity
would have skipped a box whose remote had just moved. The incomplete record a
push writes first goes through the same sequence, so while a push is in
flight every other machine sees an unknown identity and pays the real check,
which is what reports the interrupted push if it never completes. The
bulk listing over `sync_records/` therefore shows each part's **current record
ULID in a filename** — an exact generation token, no timestamps, no sizes, no
stamps. Markers are additive: a machine on an older boxyard never writes one,
doctor's record scanner (`endswith(".rec")`) ignores them, and every rename /
delete / purge that moves or removes the records directory carries them along.

### Provable, per part

A part is provable when ALL of:

1. **Remote identity known and equal**: the listing shows exactly one marker
   for the part, its ULID is a well-formed 26-character Crockford ULID, and it
   equals the ULID of this machine's local record for the part. (Two markers,
   none, or a malformed name — e.g. rclone's `<name>.<hash>.partial` upload
   residue, one of which sits on the live remote today — ⇒ needed.)
2. **Local record complete**: the local record parses and `sync_complete` is
   true.
3. **Local tree unchanged**: `local_tree_differs(...) is False` against the
   fingerprint baseline bound to that same ULID (the machinery from 0.8.0/0.8.3;
   `None` — no usable baseline — is never proof). META's baseline is
   `meta.base.json` under `filter_signature(None)`, CONF's likewise, DATA's
   under the box's effective exclude file.

Why this is exact where the stamp was not: identity equality is compared
between the remote's CURRENT record and the local record this machine holds.
A foreign push mints a fresh ULID and rewrites the marker, so the equality
fails the moment the remote moves — there is no observation window to race,
because nothing is "stamped": the comparison is recomputed from live listing
and live local state on every pass. The `sync_checks/` stamps remain in use
only for the restic pointer (unchanged logic) and cadence.

### The special cases

| case | verdict |
|---|---|
| DATA, box placement EXCLUDED here (`get_box_checkout_status(...).state is LocalCheckoutState.EXCLUDED` — tested explicitly; `check_included()` is false for MISSING/UNAVAILABLE too and must not be used) AND no directory exists at the DATA path | provable with no signal — the real path could only answer EXCLUDED |
| DATA, placement EXCLUDED but a directory exists at the DATA path | needed — the real path reports the unexpected tree |
| DATA, placement MISSING / UNAVAILABLE / RELOCATING | needed — the real path keeps raising loudly |
| DATA, restic box | existing pointer + `tree_touched_since` logic, unchanged |
| CONF, nothing under the remote `boxes/<box>/conf/` (observed via `+ /*/conf/**` at `max_depth=3` in the `boxes/` listing) AND no local `conf.rec` AND no local conf directory — whether or not the remote holds a `conf.rec` | provable — never-had-CONF, or (**v4**) recorded-but-never-materialized: `new_box` pushes an EMPTY conf/, which rclone records but never creates on the remote, so every box looks like this from every machine but its creator. Measured: `get_sync_status` reads both-sides-absent as SYNCED whatever the records say, and a pull whose source is missing returns silently. Deliberately does not require a marker: the bootstrap can only publish one for a part this machine holds a record for, so a remote written by an older boxyard would never converge here |
| CONF, any other asymmetry (remote conf tree without a record or with one this machine has never pulled, local dir or record the remote lacks, …) | needed — the real path pulls, warns or errors, and must be allowed to |
| any part, box in the bulk tombstone list (loaded BEFORE the filter) | needed — `sync_box` prints the tombstone warning as today, even when the delete's purge failed and the box is still fully listable |
| any part, evaluating the box raised | needed, exception printed to stderr with the box name; the pass continues (one unreadable directory must not abort 632 boxes) |

### Dependency closure (the second review's finding 5)

`sync_box -c data` syncs META and CONF too (ownership is read from META;
`conf/.rclone_*` decide what DATA syncs). A skip must prove the closure of what
the command would execute, not the parts it would display:
`closure(DATA) = {META, CONF, DATA}`, `closure(CONF) = {CONF}`,
`closure(META) = {META}`. A `-c data` pass therefore cannot skip a box whose
remote CONF moved, even with a perfect DATA proof. The full pass's closure is
everything.

### Flags

`--skip-unchanged` covers the whole box; `--skip-unchanged-meta` stays as the
META-only form for the fast loop. `test_the_data_flag_does_not_switch_on_meta_skipping`
pins the old semantics and is rewritten deliberately. The "proves nothing"
message and help text change with it.

### Bootstrapping markers on a fleet that has none

No remote record has a marker today. A box becomes skippable once its current
record has one, which the next push writes. Idle boxes never push, so they
would never converge — the same trap the D1 work closed for baselines. So the
real path bootstraps them: after a pass, for every box that went through
`sync_box` and whose part came back SYNCED with matching records, if the pass's
listing showed no marker for that part, `multi-sync` writes the marker for the
remote ULID it just read (`SyncStatus.remote_sync_record.ulid`). One tiny
upload per part, once ever. Non-owners write markers too — a marker asserts the
remote's own record identity, which was READ from the remote; it is not a push.
Crash between record and marker: no marker ⇒ needed; an old marker beside a new
record ⇒ two markers ⇒ needed. Both are the loud direction.

`convert` deletes the remote `data.rec` and must delete its marker too;
`force-push`, `sync_helper` and every other writer go through `rclone_save`, so
they get markers for free. `rclone_save` also starts RAISING on a failed copy —
it discarded `rclone_copyto`'s result, which under any skip filter turns a
failed remote record write into a hidden wedge (pre-existing violation of the
loud-failure rule; fixed regardless).

## Baseline production must not lie (the second review's finding 4)

A skip trusts the baseline, so the three windows in which a baseline could
describe a tree the remote does not hold are closed:

1. **Pull racing-write guard** used the newest surviving FILE mtime, which a
   mid-pull deletion (or rename, chmod, symlink edit) does not move. v3
   proposed `tree_touched_since` (ctime and directory mtimes) against the
   pull's start; **v4: that refuses EVERY pull**, because the pull's own writes
   move ctimes and directory mtimes too (measured: not one pull recorded a
   baseline, and no timestamp gate can tell the pull's writes from a racing
   one). A directory pull now blesses only after `rclone check` proves local
   and remote equal under the transfer's own filters — the standard
   `_verify_then_bless_data` already applies — and a single-file pull (META)
   keeps the file-mtime guard, which is exact for one file. The check is one
   listing of a box that just transferred; without it the box sits on the mtime
   fallback and `_verify_then_bless_data` pays the same listing next pass. What
   the check cannot see is stated: an exec-bit-only change racing a directory
   pull is blessed as it landed.
2. **Non-owner probe-clean baseline** was fingerprinted AFTER the remote probe;
   an edit landing in between was blessed. It is fingerprinted BEFORE the probe,
   like `_verify_then_bless_data` already does.
3. **Pull adopted the remote record read AFTER the transfer**; an owner push
   between transfer and read adopted U2 with U1's downloaded tree. The pull now
   adopts the record the pre-transfer status read. If the remote moved
   meanwhile, the next pass reads NEEDS_PULL and pulls again (a no-op transfer)
   — the loud direction.

What the fingerprint cannot see is stated, not hidden: it is transport-metadata
equality, so a same-size, same-mtime, same-exec-bit content replacement is
invisible to it and to rclone alike.

## Defence in depth: the daily unfiltered pass

The supervisor loop runs one UNFILTERED pass every 24 h (a timestamp file
beside the script; no flag on that iteration). It bounds any unknown-unknown —
a lying baseline, a marker bug, an operation nobody thought of — to one day,
and it is the authoritative audit that doctor's prefiltered `diverged-box` is
not. Cost: one 45-minute pass a day, against ~70 cheap ones.

## What it should cost

Two bulk listings at pass start, measured against the live Hetzner box:
`sync_records/` 13.2 s (1,869 entries), `boxes/` 13.0 s (633 entries; the conf
subtree adds a few hundred). SFTP has no `ListR`, so each is one ReadDir per
directory inside one SSH session — ~13 s each, not "one round trip". No
pass-end listing (nothing is stamped). Per PUSHED part, the two remote record
writes each grow from one call to three (sweep, record, marker): about +2 s on
SFTP per part that actually pushed, nothing on an idle box. Per PULLED
directory part, one `rclone check` listing (v4, above). Plus a local fingerprint walk per
included part: warm-cache `rclone lsf` on 21k files took 0.24 s; the large
boxes cost seconds each, budget tens of seconds for 150 included boxes. Needed
boxes fingerprint twice (filter, then `get_sync_status`) — accepted. Marker
bootstrap: one tiny upload per part, once ever. An idle pass ≈ **2–5 min** is
the hypothesis to measure on the first rollout pass, not to quote before it.

## Verification, in order

Status per step is kept here as it happens.

1. **Tests** — DONE 2026-09-25: `tests/unit/models/test_record_markers`
   (parsing, projection, closure, write order incl. the crash window),
   `tests/integration/cmds/test_skip_unchanged` (55 tests: every row of the
   table, all ten change shapes, flags, isolation, placement, CONF states,
   tombstone, bootstrap, the two in-pass foreign-push races, closure,
   denied-then-undo, the post-probe edit, the restic pointer race, convert),
   `test_meta_skip_filter` (cost property, META decision table, failed sync
   publishes nothing), `test_mid_transfer_writes` (mid-pull deletion, mid-pull
   foreign push). Mutation-checked in the course of writing: the v3 pull guard
   went red on `test_a_quiet_pull_still_records_a_usable_baseline` and the fleet
   fixture, and the v3 marker order was caught by reasoning through the crash
   window while writing `test_the_sweep_precedes_the_record_write`.

1. **Tests**, each mutation-checked (delete the rule ⇒ its test goes red):
   every row of the special-cases table; marker parsing (partial residue, two
   markers, malformed); the injected foreign-push race in META, CONF and DATA
   variants (must be needed next pass — the v1 mechanism goes red on it); a
   denied/conflicting part followed by undoing the local edit (must still pull);
   `-c data` with only remote CONF or META changed (closure); tombstone with a
   failed purge; the three baseline-production windows (mid-pull deletion,
   post-probe edit, mid-pull foreign push), asserting remote and local FILE
   state, not only `get_sync_status`; lone local deletion/rename/chmod/symlink
   on a settled box; an unreadable directory in one box among many.
2. **Oracle on the live yard, read-only**: on mymain, after one bootstrapping
   pass, compute the filter's verdict for all 632 boxes; for every box it calls
   skippable, run the full `get_sync_status` for every part and assert SYNCED /
   EXCLUDED / never-had-CONF. Any disagreement blocks the rollout. This is a
   point-in-time check; the injected-push tests and the daily unfiltered pass
   cover what it cannot see.
3. **Adversarial code review** of the implementation by both reviewers before
   rollout.
4. **Staged rollout**: mymain first, watching at least two passes for duration
   and doctor `diverged-box`, including one pass during which another machine
   pushes a box mymain already processed in that pass; then the fleet via myrig.

## Not in scope

- Cadence policies (`--due-only`) — orthogonal, still available.
- Restic DATA — keeps its pointer-stamp logic; six boxes, separately tested.
- Teaching doctor's `diverged-box` to compare marker ULIDs from the listing
  (a free, exact divergence check that would replace its 5 s prefilter) — a
  natural follow-up once markers exist fleet-wide.

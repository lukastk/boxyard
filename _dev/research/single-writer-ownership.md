# Research: single-writer box ownership for boxyard

Ticket: *RESEARCH: single-writer box ownership* (`dc6b9a1e-efbf-43c2-9bfe-aa3d41a60626`).
Date: 2026-08-23. Branch: `research/write-ownership`, off `main` at v0.4.4.

> **Status: RESEARCH ONLY.** Nothing in `pts/` or `src/` was changed. Every
> claim marked **[verified]** was reproduced in a throwaway harness against two
> isolated boxyards sharing one local `alias` remote (the existing
> `tests.integration.conftest.create_boxyards(num_boxyards=2)` fixture). The
> user's real yard was read, never written.

## TL;DR

- **The premise is right: the multi-machine feature is unsafe, and ownership is
  the right fix.** A box included on two machines has no coordination at all
  today; the only thing standing between you and divergence is that you
  remember which machine you last used.

- **But the ticket's analysis #2 — "the distributed-lock problem solves itself"
  — is wrong. [verified]** Two machines claiming ownership *simultaneously* is
  plain last-write-wins: in 6 concurrent-claim trials, **5 saw both META pushes
  succeed** and the remote simply kept whichever landed last; only 1 produced a
  CONFLICT. The existing machinery catches divergence **separated in time**, not
  a race. It is a good backstop, not a compare-and-swap. The design must
  therefore **verify a claim by reading it back**, and must not present claiming
  as atomic.

- **The loser of that race reverts silently. [verified]** The machine whose
  claim was overwritten sees META as `needs_pull`, not `conflict`, and its next
  ordinary sync quietly replaces its own claim with the winner's. Convergence to
  a single owner is the *safe* outcome — but a `boxyard claim` that printed
  "ok" and did not stick is not acceptable, hence the read-back.

- **The schema break is worse than "fails to parse". [verified]** An unknown key
  in `boxmeta.toml` doesn't just error — `create_boxyard_meta` **skips** the box,
  so on an older machine it **disappears from `boxyard_meta.json`**, and with it
  from `boxyard list`, from `~/g` (its symlinks are actively deleted), and from
  `boxyard multi-sync`, which iterates that cache. The box stops syncing
  **silently**. Worse, `sync-missing-meta` writes the file to disk and *then*
  crashes validating it, leaving no META sync record — a state that is
  `SyncCondition.ERROR` and **does not heal after upgrading**; it needs a manual
  `boxyard sync -c meta --sync-direction pull --sync-setting force`. [verified]

- **Recommended schema:** a new `write_owner: str | None = None` on `BoxMeta`,
  **plus an unknown-key passthrough** so that this is the *last* breaking
  `boxmeta.toml` change. `save()` must **omit** the key when `None`, so phase 1
  rewrites nothing and the 583 existing boxmetas stay byte-identical.

- **Machine identity: `get_hostname()` is unusable, and the live yard proves
  it.** The `syncer_hostname` values in `~/.boxyard/sync_records` contain both
  `lukas-pocket4` **and** `pocket4` — *one physical machine under two
  identities* — alongside `Lukas’s MacBook Pro` and `Tom’s Mac Studio` (macOS
  pretty names, complete with U+2019 apostrophes, and one of them not even the
  user's name). Use a new **`machine_name` config key** carrying the canonical
  short name already used everywhere else in the my-suite (`macbook`,
  `macstudio`, `mymain`, `termux`, `ideapad`, `pocket4` — myrig's
  `[machines.*]`, `SESH_MACHINE`, and the `ctx/<machine>` box groups). Never
  derive it.

- **The refusal must not be an exception.** `boxyard multi-sync` runs on a
  20-minute supervisor loop (`~/.supervisor/scripts/boxyard-sync.sh`), catching
  every per-box exception and printing `Error`. A raise-on-every-sync design
  produces ~72 identical errors per machine per day — the exact failure the
  v0.4.x work spent a week eliminating. Instead: a non-owner **pulls quietly**
  when clean, and when it has real local changes the box enters a named,
  *reported-once* state that `doctor` surfaces with two named escapes.

- **Excluded debris would break the feature outright, and does so today in a
  smaller way. [verified]** Creating `.DS_Store`, `__pycache__/x.pyc` or
  `.venv/pyvenv.cfg` in a box flips it to `needs_push` — even though all three
  are in `DEFAULT_RCLONE_EXCLUDE` and the resulting push transfers **nothing**.
  `get_sync_status` asks `check_last_time_modified`, which walks the tree with
  no filters. On a read-only machine that means "you have local changes" for
  changes that do not exist. Since Lukas named OS debris as a *cause* of the
  problem, the design must ask *"would a push actually transfer anything?"*, not
  *"is the mtime newer?"*.

---

## 1. What is true today

### 1.1 The parts, and the order they sync in

`sync_box` (`pts/mod/cmds/03_sync_box.pct.py`) syncs a box in three
independently-recorded parts, **in this order**:

| order | part | local path | remote path | sync record |
|---|---|---|---|---|
| 1 | `META` | `local_store/<sl>/<index>/boxmeta.toml` | `<store>/boxes/<index>/boxmeta.toml` | `meta.rec` |
| 2 | `CONF` | `local_store/<sl>/<index>/conf/` | `<store>/boxes/<index>/conf/` | `conf.rec` |
| 3 | `DATA` | `~/dev/<index>` | `<store>/boxes/<index>/data/` | `data.rec` |

That ordering is a gift: **META is already fetched before DATA is pushed**, so
"who owns this box?" can be answered from freshly-synced state at exactly the
moment the push decision is made. No new round-trip is needed.

One implementation trap: `sync_box` loads `box_meta` from `get_boxyard_meta`
*before* the META sync and never re-reads it (it only calls
`refresh_boxyard_meta` at the very end). After a META **pull** that object is
stale. The ownership check must re-read `BoxMeta.load(...)` from disk.

### 1.2 The conflict machinery is a backstop, not a lock — [verified]

The ticket's analysis #2 argued that because two simultaneous claims are two
divergent edits to the META part, boxyard's existing CONFLICT detection makes
the distributed lock unnecessary. Half of that is right.

**Sequential claims are caught.** M1 claims and pushes META; M2 (stale) claims
and pushes: M2 is refused with `SyncUnsafe`, condition `conflict`. And because
the exception propagates out of `sync_box`, **the whole box sync aborts before
DATA** — M2's data was verified not to reach the remote. [verified]

**Simultaneous claims are not.** Running both machines' META pushes
concurrently, six times:

```
trial 0: m1=ok           m2=ok           remote owners=['own/m2']
trial 1: m1=ok           m2=ok           remote owners=['own/m2']
trial 2: m1=ok           m2=ok           remote owners=['own/m1']
trial 3: m1=ok           m2=ok           remote owners=['own/m1']
trial 4: m1=ok           m2=ok           remote owners=['own/m1']
trial 5: m1=ok           m2=SyncUnsafe   remote owners=['own/m1']
summary: {'m1': 3, 'm2': 2, 'conflict': 1}
```

**5 of 6 trials were last-write-wins.** The reason is structural, not a bug:
`sync_helper` computes `get_sync_status` and *then* transfers. Everything
between those two points is a TOCTOU window, and SFTP offers no
compare-and-swap to close it.

**And the loser reverts silently.** [verified] After M1's claim was overwritten,
M1's META condition is `needs_pull` — **not** `conflict` — because a completed
push writes a *fresh* `SyncRecord` whose timestamp is newer than the
`boxmeta.toml` mtime, so M1 no longer looks "locally modified". M1's next
ordinary sync pulls M2's boxmeta and M1's own claim is gone, with no message.

Consequences for the design, and they are not small:

1. `boxyard claim` must **push, then read the remote boxmeta back**, and fail
   loudly if it does not name this machine. This shrinks the window to the gap
   between the two pushes and, more importantly, makes both racers *notice*.
2. The documentation must not claim atomicity. Ownership converges to a single
   writer; it is not linearizable.
3. Within the race window both machines briefly believe they own the box, so a
   concurrent DATA push is possible — at exactly today's risk level. Ownership
   removes the *routine* danger (two machines editing over days); CONFLICT
   detection remains the backstop for the residual race.

### 1.3 The schema constraint, measured — [verified]

Every model inherits `const.StrictModel` (`ConfigDict(extra="forbid")`), and
`BoxMeta.load` splats the parsed TOML straight into the constructor. Adding a
`boxmeta.toml` key therefore breaks older readers. What that actually looks
like, in order of severity:

- **`BoxMeta.load()`** → `ValidationError: Extra inputs are not permitted`.
- **`create_boxyard_meta()`** → prints a `Warning: skipped 1 unreadable box
  registration(s)` to **stderr** and returns without the box. So
  `boxyard_meta.json` loses it, and with it:
  - `boxyard list` and everything built on it (including myrig's `boxyard-pick`,
    `mcd`, the sesh cockpit's box pickers);
  - `create_user_box_group_symlinks`, which rebuilds `~/g` from that cache and
    **deletes** symlinks not in its list — the box vanishes from `~/g`;
  - **`boxyard multi-sync`**, which iterates `boxyard_meta.box_metas`. The box
    **stops syncing entirely, with no error at all.** Under a supervisor loop
    that stderr warning goes to a log file nobody reads.
- **`sync-missing-meta`** → `rclone_sync` writes *all* the missing boxmetas to
  disk first, then per-box tasks build their META sync records; the unparseable
  one raises out of `async_throttler` **after** the file is on disk and
  **before** its record is written. (Other boxes in the same batch are
  unaffected — `async_throttler` runs everything and raises afterwards — but the
  trailing `refresh_boxyard_meta` is skipped.) The next pass no longer sees the
  box as "missing", so it never retries. Net effect: one crash in the 15-minute
  meta-sync loop, then permanent silence.
- **After the machine is finally upgraded**, that box is *still* broken:
  boxmeta present, remote present, **no local sync record** →
  `SyncCondition.ERROR` — *"Something wrong here. Local sync record does not
  exist, but the local and remote path exists"* — and `sync_box` raises. It does
  **not** self-heal. [verified]
  Recovery is `boxyard sync -r <box> -c meta --sync-direction pull
  --sync-setting force`, which works. [verified]

**This is the single strongest argument in the whole document**, and it points
at a fix bigger than this feature: `boxmeta.toml` needs to be *forward*
compatible, so that this is the last time a schema addition can do this.

### 1.4 `get_hostname()` is not a machine identity

`_utils/base.get_hostname()` returns `scutil --get ComputerName` on macOS and
`platform.node()` elsewhere. The live yard shows what that produces:

```
sync_records/*/*.rec — syncer_hostname:        boxmeta.toml — creator_hostname:
  446  Lukas’s MacBook Pro                       463  Lukas’s MacBook Pro
  296  mymain                                    110  mymain
    3  Tom’s Mac Studio                            5  Tom’s Mac Studio
    2  lukas-pocket4                               2  lukas-pocket4
    2  lukas-ideapad                               2  lukas-ideapad
    1  pocket4                                     1  pocket4
```

Three separate problems, all real:

1. **`lukas-pocket4` and `pocket4` are the same machine.** A hostname change
   silently minted a second identity. Under ownership that is a box you can no
   longer write to from the machine that owns it.
2. macOS pretty names are display strings — spaces, a U+2019 apostrophe, and
   `Tom’s Mac Studio` for a machine the user calls `macstudio`. They are
   user-editable in System Settings.
3. The same drift already happened to the `ctx/<machine>` box groups:
   `ctx/remote` (30 boxes) and `ctx/mac` (2) are fossils of earlier names.

`creator_hostname` should be **left exactly as it is** — it is a historical
record, 583 boxmetas carry these values, and rewriting them is out of scope.
`write_owner` gets a *different*, stable identifier.

### 1.5 Excluded debris flips a box to `needs_push` — [verified]

```
M2 DATA right after include: synced
after creating .DS_Store            -> DATA condition = needs_push
after creating __pycache__/m.pyc    -> DATA condition = needs_push
after creating .venv/pyvenv.cfg     -> DATA condition = needs_push

...and does a sync of that debris actually transfer anything?
  sync_box DATA -> condition=needs_push, synced=True
  remote DATA now: ['.git', 'file.txt']      <- nothing was transferred
```

All three are in `const.DEFAULT_RCLONE_EXCLUDE`. `get_sync_status` calls
`check_last_time_modified(local_path)`, a raw filesystem walk with **no
filters**, so anything that touches the tree flips the condition. Today that
costs a pointless rclone round-trip. Under a naive ownership design it means a
read-only machine reports *"you have local changes"* forever, for changes that
will never be pushed — and since Lukas explicitly named OS debris as a cause of
the multi-machine mess, this alone would sink the feature.

### 1.6 `doctor` has no CONFLICT check at all

The 14 checks in `DOCTOR_CHECK_NAMES` cover interrupted syncs, orphaned records,
stale caches, tombstones and more — but **nothing reports a box in CONFLICT**.
The two boxes wedged on macbook are invisible to `doctor`; they surface only as
recurring red lines in the `multi-sync` board. Ownership adds states that need
reporting, so the conflict check should be added in the same pass.

### 1.7 The hints are the deliverable, not an afterthought

The ticket calls out `duplicate-box-id`'s hint. It is worse than vague — it is
dangerous:

> *"Box ids must be unique; inspect the duplicates and delete or re-create one
> of them."*

`delete_box` **purges the remote and writes a tombstone keyed by `box_id`**. For
two registrations sharing an id, deleting "one of them" tombstones the id, and
every machine that syncs the *other* box then sees `TOMBSTONED`. The hint
recommends an action that destroys both. Every hint written for this feature
must name an exact command that is safe to run as written.

---

## 2. Recommended design

### 2.1 Schema — `write_owner`, plus an unknown-key passthrough

```toml
# boxmeta.toml — an owned box
storage_location = "hetzner-box"
creator_hostname = "Lukas’s MacBook Pro"
groups = [ "mysetup", "ctx/mymain",]
parents = []
write_owner = "mymain"          # <- new; ABSENT when unowned
```

```python
class BoxMeta(const.StrictModel):
    ...
    parents: list[str] = []
    write_owner: str | None = None      # machine_name of the single writer
    unknown_keys: dict[str, Any] = {}   # forward-compat passthrough, see below
```

Four rules make this work:

1. **`save()` omits `write_owner` when it is `None`.** `save()` already deletes
   three derived keys from `model_dump()`; add a fourth deletion. Consequences:
   phase 1 rewrites nothing, all 583 existing boxmetas stay byte-identical, and
   `boxyard release` returns a file to the old format so old machines can read
   it again.
2. **`unknown_keys` is a round-trip passthrough, not `extra="ignore"`.**
   `BoxMeta.load` splits the parsed TOML into known and unknown keys; `save()`
   merges the unknown ones back verbatim. `extra="ignore"` would be actively
   dangerous — an older machine would silently **strip** a newer machine's
   `write_owner` on the next `add-to-group`. Passthrough preserves it, and
   `doctor` reports its presence (`unknown-boxmeta-keys`) so it is loud rather
   than hidden. **This makes v0.5.0 the last release that a `boxmeta.toml`
   addition can break.**
3. **Validate the value.** `write_owner` must match `[A-Za-z0-9_-]{1,64}` — no
   `/`, no spaces, no control characters. Unlike `creator_hostname`, this is a
   key the system compares, not a label it prints.
4. **One field, not two.** `write_owner_since` is tempting for staleness
   reporting, but once the passthrough exists it can be added later for free.
   Ship the minimum.

### 2.2 Machine identity — a required-to-claim `machine_name` config key

```toml
# ~/.config/boxyard/config.toml
machine_name = "mymain"
```

- **Never derived.** No `get_hostname()`, no `/etc/machine-id` (absent on macOS
  and termux, and it changes on reimage), no self-assigned random id (unreadable
  in an error message, and it would need its own sync channel).
- **Canonical short names**, the ones already in myrig's `[machines.*]` and in
  `SESH_MACHINE`: `macbook`, `macstudio`, `mymain`, `termux`, `ideapad`,
  `pocket4`. myrig already templates this exact value into
  `~/.myrig/zshenv/boxyard.sh` as `DEFAULT_BOX_GROUPS='["ctx/{{ current }}"]'`,
  so wiring it into `config.toml` is a one-line jinja change on a path that
  already exists.
- **Optional in the model, required to claim.** Making it a required config key
  would break every machine's config on upgrade until myrig runs. Instead:
  `boxyard claim` refuses without it, naming the key; `doctor` reports
  `machine-name-unset`; and a machine with no name is simply never the owner,
  which is the safe direction and has *zero* effect until boxes start being
  claimed.
- `BOXYARD_MACHINE_NAME` overrides it, for tests and one-offs. This follows the
  existing `BOXYARD_CONFIG_PATH` / `BOXYARD_RCLONE` precedent.

sesh reached the same conclusion the hard way: its daemon **refuses to run on a
guessed identity** rather than fall back to a hostname
(`sesh/internal/daemon/daemon.go`).

### 2.3 Semantics — unowned means unrestricted; opt-in per box

**Unowned = today's behaviour, unchanged.** Not first-writer-claims: that would
silently assign 583 boxes to whichever machine synced first and instantly lock
five machines out of boxes they legitimately use, which is a mass state change
nobody asked for. Not "unowned is read-only" either — that bricks the yard.

Ownership is therefore **opt-in per box**, and there is no global on/off switch:
the switch *is* whether a box has an owner. A box that is never claimed behaves
in v0.5.x exactly as it does in v0.4.4.

**But the migration should claim in bulk once**, and this is the elegant part:

```
boxyard claim --all-included          # on each machine, once, after the rollout
```

A box is normally included on exactly one machine, so one pass per machine
assigns correct owners across the yard (~116 boxes on mymain, ~450 on macbook).
And where a box *is* included on two machines, the second machine's claim hits a
META **CONFLICT** — the sequential case that provably works — so **the migration
itself enumerates precisely the boxes that were at risk all along.** That is the
answer to "which of my 583 boxes are actually double-included?", which nothing
can answer today.

### 2.4 Where the refusal lives, and why it is not an exception

**Location:** in `sync_box`, after the META sync and before the CONF/DATA syncs.
Not in `sync_helper` — that function takes paths and knows nothing about boxes,
and pushing box semantics into it would be the wrong layer.

**Behaviour**, for a box whose `write_owner` is set and is not this machine:

| DATA condition after META sync | action |
|---|---|
| `SYNCED` | nothing, silently |
| `NEEDS_PULL` | **pull**, silently — this is a read-only replica doing its job |
| `NEEDS_PUSH` | run a **filtered dry-run probe** (§2.5). Nothing to transfer → treat as synced, silently. Something to transfer → **`WRITE_DENIED`** |
| `CONFLICT` | **`WRITE_DENIED`** |
| `TOMBSTONED` / incomplete | unchanged — these short-circuit earlier |

`WRITE_DENIED` is a new `SyncCondition` member that `sync_box` substitutes into
its return value for that part. `get_sync_status` is **not** touched — it stays
a pure function of paths and records. The part is simply not synced; META keeps
syncing, other boxes are unaffected, and **nothing raises**.

Why not raise: `multi-sync` runs every 1200s under supervisor
(`~/.supervisor/scripts/boxyard-sync.sh`) and catches every per-box exception
into a red `Error` line. Raising would produce ~72 identical errors per machine
per day, which is precisely the pathology the v0.4.0–v0.4.4 work existed to
kill — an error that recurs forever, cannot be resolved, and trains you to
ignore the tool.

Instead the state is **reported once, by `doctor`**, and shown by `multi-sync`
as a distinct non-error status (yellow `Read-only` or `Write denied`, not red
`Error`).

**META stays writable by every machine.** It has to: otherwise ownership can
never be transferred, and `groups` / `parents` could not be edited from a
non-owner. The asymmetry has exactly one hole — a non-owner can push a META that
*clears* `write_owner`, an unguarded steal-by-release. The `release` command
refuses to run on a box this machine does not own, so it takes deliberate
hand-editing to reach; and the outcome is "unowned", i.e. today's behaviour, not
data loss. Worth stating; not worth extra machinery for a single-user system.

**CONF follows DATA.** `conf/.rclone_include|.rclone_exclude|.rclone_filters`
determine what DATA syncs, so letting a non-owner push CONF would let it change
the owner's sync filters. Non-owners pull CONF only.

**If `DATA` is in `sync_choices`, `META` is synced first regardless.** Otherwise
`boxyard sync -c data` would decide ownership from a possibly-stale local
boxmeta — the one path where a non-owner could push without ever learning it had
been claimed.

### 2.5 The dry-run probe, and the deeper fix

On the non-owner `NEEDS_PUSH` path, ask rclone what a push would actually do,
with the box's real filters — `_sync` in `sync_helper` already takes `dry_run`
and builds the identical filter set. If nothing would transfer, the box is
clean; the `.DS_Store` costs one extra rclone listing and no user-visible noise.

This is deliberately the *narrow* fix. The **deeper** one is that
`check_last_time_modified` should honour the box's exclude/filter rules, which
would also delete today's pointless no-op pushes across the whole yard. It is a
bigger, riskier change — rclone's filter semantics are not trivial to
re-implement in Python, and getting it subtly wrong means silently *not*
syncing real work, the worst failure mode this codebase has. **Recommend: ship
the probe now, and raise the filter-aware mtime walk as a separate ticket.**

### 2.6 Commands

| command | effect |
|---|---|
| `boxyard claim [-r BOX]` | unowned → this machine. Writes boxmeta, pushes META, **re-reads the remote boxmeta and verifies it names this machine**; loud failure if it does not (§1.2). Refuses if owned by another machine, naming it and pointing at `--steal`. Refuses if `machine_name` is unset. |
| `boxyard claim --all-included` | the migration pass of §2.3. |
| `boxyard release [-r BOX]` | owner → unowned. Pushes META. The clean half of a handover, and it returns the file to the pre-v0.5 format. |
| `boxyard claim --steal [-r BOX]` | take ownership from another machine. Requires `--yes` or typing the box name. Prints the previous owner and states plainly that the previous owner's unpushed work will now be refused there. |
| `boxyard discard-local [-r BOX]` | the other way out: force-pull DATA over this machine's local changes, **with `delete_backup=False`** so the discarded work survives in `local_sync_backups_path`, and print that path. |
| `boxyard owner [-r BOX]` / `boxyard list --owner X` | read the state. `list` already renders `groups`; add an owner column and a filter. |

**The clean handover is `release` on A, then `claim` on B** — two online steps,
no force, no race. `--steal` exists for when A is offline, dead, or reformatted,
and should read like the deliberate act it is.

`boxyard include` is where Lukas's "read-only by default" lands:

- box **owned by another machine** → include, and DATA is pull-only from then
  on. Print it: *"included read-only — `<machine>` is the write owner."* This is
  the safe default he asked for, and after the §2.3 migration it is what the
  second machine will actually hit.
- box **unowned** → include as today, but print a one-line nudge naming
  `boxyard claim`.
- `boxyard include --read-only` → include without claiming, for an unowned box
  you only want to read.

### 2.7 `--sync-setting force` must **not** override ownership

`force` is a *sync-safety* override ("I accept overwriting"). Ownership is a
*coordination* statement ("another machine is the writer"). Conflating them
means the muscle-memory `--sync-setting force` used to unstick a box silently
steals ownership — and worse, leaves the remote holding B's data while
`boxmeta.toml` still says A owns it. That is a lie in the shared state, which is
strictly worse than a refusal.

So: **ownership is checked before, and independently of, `sync_setting`.** No
`--ignore-ownership` bypass flag either; a bypass that leaves boxmeta stale is
the hack this rule exists to prevent. `boxyard claim --steal` is one command and
does the right thing, and the refusal message says so.

`force-push` (`cmds/13_force_push_to_remote.pct.py`) bypasses `sync_helper`
entirely and must therefore carry its own check. Same for `rename --scope
remote|both` (it renames the remote directory) and `delete` (it purges the
remote and writes a tombstone).

### 2.8 `doctor`

New checks, with hints that name exact, safe commands:

- **`write-denied`** — *"Box '<index>' is owned by '<machine>' but has local
  changes here that will never be pushed."*
  Hint: *"Either take over the box with `boxyard claim --steal -r '<index>'`, or
  throw away the local changes with `boxyard discard-local -r '<index>'` (which
  keeps a copy under `<backups path>`). Until then the box only pulls."*
  Two options, both named, both safe to run as written.
- **`box-conflict`** — the gap from §1.6: report any part in `CONFLICT`.
  Remote check, skipped with `--no-remote`. This one is worth shipping even if
  ownership is deferred: it would have surfaced the two wedged macbook boxes.
- **`unknown-boxmeta-keys`** — a boxmeta carries a key this version does not
  know. Hint: *"This box was written by a newer boxyard. The key is preserved
  untouched. Upgrade this machine (`<version>`) to use the feature it belongs
  to."*
- **`machine-name-unset`** — `machine_name` missing while any box on this
  machine is owned. Hint names the config key and the myrig template.
- **`stale-owner`** *(optional)* — box owned by a machine that is not in the
  local machine list.

### 2.9 Interactions

- **Tombstones** — a tombstoned box short-circuits `sync_box` before any
  ownership logic, so a delete always wins over ownership; no interaction. But
  `delete` itself must be owner-gated (see §2.7).
- **Remote index** — `find_remote_box_by_id` maps `box_id` → remote index name
  for renamed boxes. Ownership lives *inside* boxmeta, keyed by box, so renames
  carry it automatically. No change.
- **`sync-missing-meta`** — pure pull; no gate needed. It is the mechanism by
  which ownership becomes visible for the ~470 boxes *not* included on a given
  machine, which is what lets `boxyard list --owner` answer fleet-wide questions
  offline. Its one change is inheriting the tolerant parse of §2.1.
- **`_fast`** — reads `boxyard_meta.json` as plain dicts with `.get()`, so it
  keeps working untouched; add `write_owner` to `_to_result` to expose it.
- **`multi-sync`** — needs the new status colour (§2.4) and nothing else.

---

## 3. What this does **not** solve

Stated plainly, because a feature that oversells itself is worse than no
feature:

1. **It is not a lock.** Simultaneous claims are last-write-wins, measured at
   5-in-6 (§1.2). Ownership converges; it does not serialize.
2. **CONFLICT detection is still load-bearing.** Interrupted syncs, `--steal`
   handovers, and the race above can all still diverge. This feature *reduces
   the rate* at which conflicts are manufactured; it does not remove the need to
   detect and resolve them. If anything it raises the priority of the
   `box-conflict` doctor check (§1.6).
3. **It does not stop debris from appearing.** It stops debris from *blocking*
   you (§2.5), but `.DS_Store` and `__pycache__` will keep landing in boxes on
   every machine that opens them.
4. **It does not know whether a machine is alive.** `--steal` from a machine
   that is merely offline for a week is indistinguishable from stealing from one
   that is genuinely gone. A remote per-machine heartbeat
   (`<store>/machines/<name>.json` with `last_seen_utc`) would answer this and
   is ~80 lines — worth doing **later**, for the steal decision and for
   `doctor`; it is not needed for the migration (§4).
5. **It does not protect against a machine editing a box it does not have
   included.** Nothing does; that is not a reachable state.

---

## 4. Migration

The rollout is three ships, and the safety comes from one property: **nothing
writes `write_owner` until phase 2, so phase 1 cannot break anything.**

### Phase 0 — before any code (2 minutes)

`ssh-target <machine> boxyard --version` across all six. Note which are behind.
`pocket4` and `termux` lag; `termux` appears in **no** sync record or boxmeta in
the entire yard, so confirm whether it runs boxyard at all — if it does not, it
is not a constraint.

### Phase 1 — v0.5.0, "tolerate". No behaviour change.

- `BoxMeta` gains `unknown_keys` passthrough (§2.1) and the `write_owner` field,
  **defaulting to `None` and omitted by `save()`**.
- `machine_name` config key (optional), read by nothing yet except `doctor`.
- `doctor` gains `unknown-boxmeta-keys`, `machine-name-unset`, `box-conflict`.
- **No claim commands. Nothing writes the new key.**
- myrig templates `machine_name` into `config.toml`.

Roll to **all six machines**. Verify with `boxyard doctor` on each. Because no
boxmeta is rewritten, the remote is byte-identical before and after, and a
machine still on 0.4.4 sees nothing at all.

### The gap between the phases

This is the question the ticket asks to answer concretely. **If phase 2 is not
started until every machine reports ≥ 0.5.0, the gap is empty** — there is no
new key anywhere, because only claiming writes one.

If the gate is jumped anyway (someone claims while `pocket4` is still on 0.4.4),
the exact sequence on the lagging machine is:

1. `boxyard-meta-sync.sh` (every 15 min) pulls the new-format boxmeta to disk,
   then **crashes** validating it. No META sync record is written. [verified]
2. The box is now present-but-unparseable. Every `create_boxyard_meta` skips it
   with a stderr warning, so it **silently drops out of `boxyard list`, out of
   `~/g` (its symlinks are deleted), and out of `multi-sync`**. Its DATA stops
   syncing on that machine, with no error. [verified]
3. `sync-missing-meta` stops crashing on the next pass, because the box is no
   longer "missing". Total user-visible signal: one log line, once.
4. Upgrading the machine does **not** fix it: the missing sync record leaves
   META in `SyncCondition.ERROR` and `sync_box` raises. [verified]
5. **Repair:** `boxyard sync -r <box> -c meta --sync-direction pull
   --sync-setting force`, then a normal sync. [verified]

Blast radius is bounded to the boxes actually claimed during the gap. Data is
never lost — the DATA part is untouched throughout — but a box can sit unsynced
and *invisible* for as long as it takes to notice. Phase 1's
`unknown-boxmeta-keys` doctor check is what turns step 2 from silent into loud
**for every future schema change**; it cannot retroactively help a 0.4.4
machine, which is exactly why the gate matters this once.

### Phase 2 — v0.5.1, "claim". Opt-in per box.

- `claim` / `release` / `claim --steal` / `discard-local` / `owner`.
- The §2.4 refusal in `sync_box`, the §2.5 probe, `SyncCondition.WRITE_DENIED`.
- Owner gates on `force-push`, `rename --scope remote|both`, `delete`.
- `include`'s read-only default (§2.6).
- `doctor` gains `write-denied`; `multi-sync` gains the status.

### Phase 3 — the one-time claim pass

On each machine, in turn, `boxyard claim --all-included`. Expect META CONFLICTs
on genuinely double-included boxes — that is the point (§2.3). Resolve each with
`--steal` or `discard-local`, then move to the next machine.

Do the **two currently-wedged macbook boxes**
(`20260224_r0nycg__equity-local-authority-culture-spending`,
`20251201_130556_NKBnW__repos-to-obsidian-notes-v2`) by hand *before* this pass,
not during it.

---

## 5. Alternatives considered and rejected

### A — ownership as a reserved group name (`own/<machine>` in `groups`)

**Genuinely tempting, and the closest call in this document.** `groups` is
already `list[str]`, already synced in META, already conflict-detected, and
already carries machine provenance (`ctx/<machine>`, 450 boxes on `ctx/macbook`
alone). So ownership could ship with **zero schema change and zero migration
risk** — old machines parse it, sync it, and preserve it on round-trip. It would
even come with `boxyard list -g "own/mymain"` and a `~/g/own/<machine>/` tree
for free.

Rejected because it makes `groups` mean two things forever. Every future reader
must know that some group names are machinery; `add-to-group` /
`remove-from-group` must police a reserved prefix; a stray
`boxyard add-to-group own/macstudio` silently reassigns write access; and "at
most one owner" becomes a validation rule over a list rather than a property of
a field. That is the kind of overloading the repo's own guidelines call a smell,
and §2.1's passthrough removes the migration risk that was its main advantage.

**If Lukas would rather not do a two-phase rollout at all, this is the option to
take** — it is a legitimate trade, not a bad idea, and the rest of §2 applies
unchanged.

### B — ownership in a file inside the `CONF` part (`conf/owner.toml`)

Also non-breaking (old machines sync it as opaque content), and arguably the
most *honest* placement — CONF already holds per-box configuration.

Rejected because CONF is invisible to `boxyard_meta.json`, `_fast`, and
`boxyard list`, and — decisively — **`sync-missing-meta` does not fetch CONF**.
So for the ~470 boxes not included on a given machine, "who owns this?" would be
unanswerable offline, which breaks `boxyard list --owner` and the `doctor`
checks. It also adds a moving part: CONF syncs with `allow_missing_source=True`
and has its own record.

### C — a lock file on the remote (`<store>/locks/<box_id>`)

The instinct is to make ownership a real lease. But SFTP has no atomic
create-if-absent through rclone, so it is *the same* last-write-wins as §1.2
with an extra file to leave stale, plus a new class of failure (the lock
outlives the machine). Ownership-in-boxmeta at least rides machinery that
already exists, already syncs, and already has a conflict backstop.

### D — first-writer-claims / unowned means read-only

Both mass-assign state to 583 boxes that nobody chose. `first-writer-claims`
hands each box to whichever machine happens to sync first; `unowned = read-only`
locks the entire yard on upgrade. Opt-in per box (§2.3) is the only variant
where a v0.5 machine behaves exactly like a v0.4.4 machine until you say
otherwise.

### E — raise `SyncUnsafe` on the non-owner push

The obvious implementation, and the one the ticket's sketch describes. Rejected
in §2.4: at 72 supervisor passes per machine per day it manufactures exactly the
noise the last week of work removed.

### F — a fleet version registry as the migration gate

A remote `<store>/machines/<name>.json` heartbeat could make `boxyard claim`
refuse until every machine reports ≥ 0.5.0. It is ~80 lines and it is a *good
idea* — but not as a migration gate: it cannot see a machine that has not synced
since upgrading, so it is a guard against the known-bad rather than a proof, and
with six machines the manual `ssh-target` sweep in Phase 0 is two minutes and
strictly more reliable. Build it later, for the `--steal` staleness question and
for `doctor` (§3.4).

---

## 6. Corrections to the ticket's prior analysis

| # | Ticket's claim | Verdict |
|---|---|---|
| 1 | Ownership must live in `boxmeta.toml`, not `boxyard_meta.json` | **Correct.** `boxyard_meta.json` is a derived local cache, rebuilt by `create_boxyard_meta` and never synced. |
| 2 | "The distributed-lock problem solves itself" — two simultaneous claims are two divergent META edits, which boxyard already detects as CONFLICT | **Wrong as stated. [verified]** True for claims separated in time; false for a race, which is plain last-write-wins 5 times in 6, with the loser reverting *silently* (`needs_pull`, not `conflict`). The design compensates with a post-push read-back (§2.6) and by not claiming atomicity (§3.1). |
| 3 | Granularity is asymmetric — DATA owned, CONF follows, META writable by anyone | **Correct**, and the one hole (steal-by-release from a non-owner) is bounded and worth accepting (§2.4). |
| 4 | The design needs a way out, and the sketch has none | **Correct, and understated.** Beyond naming the two escapes, the *refusal itself* must not be an exception — §2.4. |
| — | (not in the ticket) | **New:** excluded OS debris flips a box to `needs_push` and would wedge every read-only machine (§1.5). |
| — | (not in the ticket) | **New:** an unknown boxmeta key makes a box *silently vanish* from `list`, `~/g` and `multi-sync` — not merely fail to parse — and does not heal on upgrade (§1.3). |
| — | (not in the ticket) | **New:** `doctor` has no CONFLICT check, so the two wedged macbook boxes are invisible to it (§1.6). |

---

## 7. Decisions for Lukas

1. **`write_owner` field (§2.1, two-phase) or `own/<machine>` group (§5A, no
   migration)?** The document recommends the field; the group is a defensible
   trade if the rollout is unwelcome.
2. **Is `termux` a boxyard machine at all?** It appears in no sync record and no
   boxmeta in the whole yard. If it is not, the fleet is five machines and Phase
   0 is easier.
3. **`boxyard claim --all-included` in Phase 3 — go, or claim boxes one at a
   time as you meet them?** The bulk pass is what surfaces the
   double-included boxes; incremental claiming never will.
4. **Ship `box-conflict` in `doctor` immediately, ahead of everything else?** It
   is independent of ownership, small, and would have caught the two wedged
   boxes months ago.

## Sources

- Code: `pts/mod/_models.pct.py` (`BoxMeta`, `save`, `load`, `create_boxyard_meta`,
  `get_sync_status`, `create_user_box_group_symlinks`),
  `pts/mod/_utils/02_sync_helper.pct.py`, `pts/mod/cmds/03_sync_box.pct.py`,
  `pts/mod/cmds/04_sync_missing_boxmetas.pct.py`,
  `pts/mod/cmds/05_modify_boxmeta.pct.py`, `pts/mod/cmds/08_delete_box.pct.py`,
  `pts/mod/cmds/13_force_push_to_remote.pct.py`, `pts/mod/cmds/14_doctor.pct.py`,
  `pts/mod/_cli/multi-sync.pct.py`, `pts/mod/const.pct.py` (`StrictModel`,
  `DEFAULT_RCLONE_EXCLUDE`), `pts/mod/_fast.pct.py`, `pts/mod/_utils/00_base.pct.py`
  (`get_hostname`, `async_throttler`).
- Live yard (read-only): `~/.boxyard/local_store/hetzner-box/*/boxmeta.toml`
  (583 boxes), `~/.boxyard/sync_records/*/*.rec`, `~/.config/boxyard/config.toml`,
  `~/dev` (116 included boxes), `~/g`.
- myrig: `config.toml [machines.*]`, `home/.myrig/zshenv/^all^boxyard.sh.jinja`,
  `home/^supervisor^.supervisor/conf.d/boxyard-sync.ini` and
  `boxyard-meta-sync.ini`, `home/^supervisor^.supervisor/scripts/boxyard-sync.sh`
  (1200 s loop), `home/.myrig/scripts/boxyard-meta-sync.sh` (900 s loop).
- sesh: `internal/daemon/daemon.go` — refusing a guessed machine identity.
- Prior art in-repo: `_dev/research/preserving-permissions.md`,
  `parity/PARITY-NOTES.md` (on `feat/go-rewrite`), CHANGELOG 0.4.0–0.4.4.
- Experiment scripts (throwaway, not committed): `exp1_conflict_as_lock.py`,
  `exp2_race_and_schema.py`, `exp3_debris_and_recovery.py`, `exp4_recovery.py`.

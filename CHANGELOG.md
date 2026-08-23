## [0.5.3] - 2026-08-23

### 🐛 Bug Fixes

- **A box's `conf/` now reaches machines that did not write it.** Per-box rclone
  filters (`conf/.rclone_include|_exclude|_filters`) decide what a box's DATA
  syncs — and they only ever existed on the machine that created them. Every
  other machine synced that box with the *global* filters instead. A box whose
  `.rclone_include` narrows what it syncs would sync **everything** on the
  second machine.

  The cause was a single branch in `get_sync_status`: absent locally + present
  remotely was read as `EXCLUDED`. That is correct for DATA, where absence
  means the box is deliberately not included here and pulling it would undo an
  `boxyard exclude`. For CONF nobody chose anything — the files have simply
  never been fetched — and reading it as `EXCLUDED` made the absence
  **self-perpetuating**: `conf/` is missing, so it is judged excluded, so it is
  never pulled, so it stays missing.

  `get_sync_status` takes paths rather than a `BoxPart`, so it could not tell
  the two apart. It now takes `local_absence_means_excluded`, threaded through
  `sync_box` → `sync_helper`, defaulting to today's behaviour and passed as
  `False` only for CONF.

  Found while reviewing release 2, and pre-existing — reproduced on an *unowned*
  box, so not caused by ownership. On the live yard it had no effect yet: only
  5 boxes have a per-box filter and each is included solely on its creating
  machine. It would have started biting during the ownership migration, which
  is what encourages including a box on a second machine.

  Cost is negligible: for the ~580 boxes with no `conf/` at all, both sides are
  absent, which already resolves to `SYNCED` with no transfer.

  **The Go port has the same branch and needs the same change.**

## [0.5.2] - 2026-08-23

Release 2 of two for single-writer box ownership ("claim"). A box may now be
**claimed** by one machine, and only that machine pushes its DATA. Ownership is
**opt-in per box**: a box nobody claimed behaves exactly as it did in v0.4.x, so
nothing changes for the 583 boxes in the yard until someone claims them.

### ✨ Features

- **`boxyard claim` / `release` / `claim --steal` / `discard-local` / `owner`.**
  `claim` makes this machine the write owner; `release` gives it up; the tidy
  handover is release-then-claim, two online steps with no force and no race.
  `--steal` exists for when the owner is gone, and says plainly that the
  previous owner's unpushed work will be refused there from then on.

  **`claim` verifies by reading the remote back.** Two machines claiming at the
  same instant is last-write-wins — measured at 5 trials in 6 — and the loser
  reverts *silently*, because a completed push writes a fresh sync record so its
  own claim then reads as an ordinary `needs_pull`. After pushing, `claim`
  re-reads the remote boxmeta and fails loudly if it does not name this machine.
  This shrinks the window; it does not close it. **Ownership converges — it is
  not a lock**, and CONFLICT detection remains load-bearing.

- **A non-owner pulls quietly and never raises.** `SyncCondition.WRITE_DENIED`
  is a condition, not an exception, and that is the whole design: `multi-sync`
  runs every 1200s under supervisor and catches per-box exceptions into a red
  `Error` line, so raising would manufacture ~72 identical unresolvable errors
  per machine per day — the exact pathology v0.4.0–v0.4.4 spent a week removing.
  A read-only replica syncs silently when clean, pulls silently when behind, and
  is reported **once**, by `doctor`, when it holds changes that will never be
  pushed. `multi-sync` shows it as a yellow `Read-only`, never as an error.

- **A dry-run probe decides whether a non-owner actually has changes.** Asking
  "is the mtime newer?" is not the same question as "would a push move
  anything": v0.4.6 stopped *literal* excludes (`.DS_Store`) from looking like
  changes, but glob patterns are deliberately not interpreted, so a
  glob-excluded file still flips a box to `needs_push`. On a read-only machine
  that would be a permanent, false "you have local changes". The probe asks
  rclone directly, under the box's real filters.

  It uses `rclone check --combined` rather than parsing `sync --dry-run`, and
  the difference is load-bearing rather than stylistic: measured, a file with
  identical content but a different mtime makes the dry run print `Skipped
  update modification time`, so any text-matching approach reports a change that
  is not one. A check that cannot be performed at all (unreachable remote)
  counts as "would transfer", so a box is reported rather than silently declared
  clean.

- **`--sync-setting force` does not bypass ownership**, and there is no
  `--ignore-ownership` flag. `force` is a sync-*safety* override ("I accept
  overwriting"); ownership is a *coordination* statement. A forced push that
  left the remote holding this machine's data while `boxmeta.toml` still named
  another owner would put a lie in shared state, which is worse than a refusal.
  The paths that bypass `sync_helper` — `force-push`, `rename --scope
  remote|both`, `delete` — carry their own gates.

- **`boxyard include` says what it means for writing.** A box owned by another
  machine prints "included read-only — `<machine>` is the write owner"; an
  unowned box gets a one-line nudge naming `boxyard claim`, suppressible with
  `--read-only`.

- **`boxyard list --owner X` / `--show-owner`**, and `write_owner` exposed
  through `_fast`.

- **Two new `doctor` checks.** `write-denied` is the *only* report of a box
  whose local changes will never be pushed — sync stays deliberately silent, so
  if doctor did not say it, nothing would. `stale-owner` reports a box whose
  owner cannot be a working owner.

### 🛡️ Three ways a box could have been frozen fleet-wide, closed

Each of these was found by reading the design against real usage, and each one
would have made a box unpushable from *every* machine, silently:

- **`claim` now refuses a box that is not included here.** A box that is not
  included still has a local registration and boxmeta — that is what
  `sync-missing-meta` maintains for the hundreds of boxes a machine does not
  hold — so claiming one would have made this machine the designated writer of
  DATA it does not have, locking out every machine that does. The refusal names
  `boxyard include` as the fix.

- **`exclude` on a box this machine owns now releases ownership** in the same
  operation. `exclude` reads as local housekeeping, but it would have left
  `boxmeta.toml` naming a machine that no longer has the box. Because releasing
  pushes META, `exclude` is now a network operation: if the remote is
  unreachable it **refuses** rather than excluding and leaving a stale owner,
  and `release` rolls its local change back so that refusal is honest. A box
  owned by another machine is unaffected.

- **`stale-owner` catches the routes nobody thought of.** Both of the above were
  found by inspection rather than by anything reporting them. The check reports
  a box owned by this machine that is not included here (exact), and a box owned
  by a name that owns nothing else while another machine owns several (a
  heuristic, and labelled as one in its hint).

### 📋 Known limitations, stated rather than implied

- **This is not a lock.** Simultaneous claims are last-write-wins. Ownership
  reduces the rate at which conflicts are manufactured; it does not remove the
  need to detect them.
- **It does not know whether a machine is alive.** `--steal` from a machine that
  is offline for a week is indistinguishable from stealing from one that is
  gone.
- **`check_last_time_modified` is still not filter-aware.** The probe works
  around that for non-owners; the underlying no-op pushes across the yard remain.

## [0.5.1] - 2026-08-23

### 🐛 Bug Fixes

- **`multi-sync` no longer opens one SFTP connection per box just to check
  tombstones.** `sync_box` asked "has this box been deleted elsewhere?" with a
  remote probe per box — 587 of them per pass, per machine, every 20 minutes,
  across six machines. That saturated the storage box's connection limit and
  was measurably failing ~8 boxes per pass on macbook, macstudio and ideapad
  with `couldn't initialise SFTP: error receiving version packet from server`.
  All three machines logged an identical count of refusals within the same
  hour — the signature of a shared server limit, not a machine-local fault.

  `multi-sync` now fetches the tombstoned ids **once per storage location**
  and tests membership in memory. The filename of a tombstone is the box id,
  so a single listing answers it for every box; the pre-existing
  `list_tombstones` could not be reused because it reads every tombstone file
  to build objects (161 `rclone cat`s on the live yard).

  Three deliberate constraints:

  - **The standalone path still probes.** `sync_box` is also a single-box
    command, and when no set is supplied it makes the individual call rather
    than skipping the check — a silent skip would turn a safety check into a
    no-op and let a box deleted from another machine be resurrected.
  - **A failed lookup raises.** An empty set means "nothing is tombstoned",
    so returning that when the remote could not be listed would be the same
    silent resurrection with no error anywhere. Only a *missing* tombstones
    directory — which genuinely means nothing has ever been deleted — yields
    an empty set.
  - **The fetch happens before any box is synced**, so a failure stops the
    pass instead of syncing part of it blind. This is one call where there
    used to be 587, so the chance of hitting a transient failure at all is far
    lower than before.

  This was found by reading the supervisor logs while investigating something
  else; the errors had been recurring every 20 minutes. They surface as errors
  at all only because v0.4.3 stopped conflating an rclone exit 1 with "path not
  found" — before that, a failed tombstone probe silently concluded "not
  tombstoned" and synced the box anyway.

## [0.5.0] - 2026-08-23

Release 1 of two for single-writer box ownership ("tolerate"). **Zero
behaviour change**: nothing in this release writes the new `boxmeta.toml` key,
nothing refuses a sync, and there are no claim commands. It exists so that
every machine can *read* the new format before any machine can *write* it.

### ✨ Features

- **`boxmeta.toml` is now forward compatible.** A key that this version of
  boxyard does not know is preserved verbatim through a load/save round-trip
  instead of making the registration unloadable.

  This is the change that matters most, because the old behaviour was far
  worse than "fails to parse". An unknown key made `BoxMeta.load` raise, and
  `create_boxyard_meta` **skips** a registration it cannot load — so on a
  machine running an older boxyard the box silently disappeared from
  `boxyard_meta.json`, and with it from `boxyard list`, from `~/g` (its group
  symlinks are actively deleted), and from `boxyard multi-sync`, which
  iterates that cache. The box **stopped syncing, with no error**, and
  upgrading the machine afterwards did not heal it: the half-finished pull
  leaves no META sync record, which is `SyncCondition.ERROR`, and `sync_box`
  then raises until someone runs an explicit forced META pull.

  The preservation is deliberately not `extra="ignore"` — ignoring would make
  an older machine silently **strip** a newer machine's key on the next
  `boxyard add-to-group`, which is a worse outcome than either. It is not
  `extra="allow"` either: that would accept a typo'd key at every construction
  site and `broken-registration` would stop catching it. Unknown keys are
  collected only when reading a file, and `doctor` reports every one of them.

  **v0.5.0 is therefore the last release that a `boxmeta.toml` addition can
  break.**

- **`write_owner` on `BoxMeta`** — the machine name of the single machine
  allowed to push a box's DATA. Nothing reads or writes it yet; the claim
  commands and the sync refusal arrive in v0.5.1. `save()` **omits** the key
  when it is unset, so an unowned box's `boxmeta.toml` is byte-identical to
  what earlier versions wrote — verified against all 583 boxmetas in the live
  yard, none of which this release rewrites.

- **`config.toml` is now forward compatible too**, for the same reason and
  with a wider blast radius. `Config` is a `StrictModel` as well, and on this
  fleet `config.toml` is one myrig-rendered artefact shared by every machine —
  so a key added for a newer boxyard would make **every command on every
  machine** fail at once, not just one box go quiet. `get_config` now collects
  unknown keys instead of rejecting the file.

  Read the limit of this carefully: **it does not rescue a machine already
  running an older version.** Tolerance has to be deployed before the key it
  tolerates, so v0.5.0 must be installed everywhere before any config gains a
  new key. Its value is forward-looking — from here on a config addition costs
  an older machine a doctor finding instead of a machine that cannot run
  boxyard at all.

  Unlike `boxmeta.toml`, the keys are not written back, because boxyard never
  rewrites `config.toml`. They are reported instead: `extra="forbid"` is what
  catches a *typo'd* config key today, and tolerating unknown keys without
  reporting them would trade a loud typo for a silent one.

  **The tolerance reaches inside the config's tables, not just its top level.**
  `[storage_locations.X]`, `[box_groups.X]` and `[virtual_box_groups.X]`
  entries are `StrictModel`s too, so covering only the top level would have
  left the identical trap one level down — and a nested addition is not
  hypothetical: `symlink_name` was added to both group models in `8d9e074`.
  Unknown keys are collected by dotted path
  (`storage_locations.hetzner-box.some_key`), so the doctor finding says where
  the key is rather than only that the file has one. The tables to walk are
  derived from the model annotations, so a config model added later is covered
  without anyone having to remember a list.

  One case is deliberately left as a loud error: an *entry* that is not a table
  at all — a key written directly under one of those containers rather than
  under one of its entries. That takes one of two forms, a dotted key before
  any `[table]` header (`virtual_box_groups.future = "x"`), or a scalar under a
  bare `[virtual_box_groups]` header. Either way it is a line the author
  believed they were putting somewhere else, so it raises and names the exact
  path rather than being quietly discarded.

  Note what does *not* reach that branch: appending a line to the end of a real
  `config.toml`. TOML lands it inside whatever table came last, and in a
  populated config that is a sub-table, so the line becomes an unknown key
  inside an entry and is tolerated.

- **`boxyard --version`** — prints the installed version and exits. It exists
  to be a rollout gate: checking a change across the fleet means
  `ssh-target <machine> boxyard --version` on each, and until now boxyard could
  not be asked at all — only `pip show boxyard`, which does not answer for a uv
  tool install. It needs no config file, so it still works on a machine whose
  config is missing or written for a newer boxyard — exactly the machines worth
  asking.

- **`machine_name` config key**, overridable by `BOXYARD_MACHINE_NAME`. This
  is how a machine will identify itself as a box's write owner. It is
  configured and never derived: `get_hostname()` cannot serve as an identity —
  one machine in this fleet has reported both `lukas-pocket4` and `pocket4`,
  and macOS reports user-editable pretty names like `Lukas’s MacBook Pro`. The
  key is optional, because requiring it would break every machine's config on
  upgrade; a machine without a name simply can never own a box.

- **Two new `doctor` checks:**
  - **`unknown-boxmeta-keys`** — a boxmeta carries a key written by a newer
    boxyard. The key is preserved untouched, so nothing is broken; this check
    is what stops that preservation from being silent.
  - **`machine-name-unset`** — no `machine_name` is configured, so this
    machine cannot own a box. Expected on every machine until its config is
    rendered with a name.
  - **`unknown-config-keys`** — `config.toml` carries a key this version does
    not know. The hint does not assume "newer boxyard": doctor cannot tell that
    from a typo, and a typo means whatever it was meant to configure is
    silently not in effect.

### 🐛 Bug Fixes

- **`boxyard add-to-group` (and every other `modify_boxmeta` caller) no longer
  writes `boxmeta.toml` from a cache of it.** It read the box meta from
  `boxyard_meta.json` — a snapshot of the last refresh — modified that, and
  wrote it back to disk, so anything that had reached `boxmeta.toml` since the
  refresh was silently overwritten with the older values. A lost update in
  general; specifically, it would have stripped a newer machine's key straight
  back out again, defeating the passthrough above. The file is now re-read
  from disk before being modified.

## [0.4.7] - 2026-08-23

### ✨ Features

- **`boxyard doctor` can now see a wedged box.** Until this release it could
  not: a box in `CONFLICT`, or one left half-written by an interrupted push
  from another machine, produced no finding at all. Two boxes on macbook sat
  wedged from March to August 2026 — five months — while `doctor` reported
  "all checks passed" on that machine every time. They surfaced only in the
  supervisor log, one line per 20-minute pass, and were found by accident.

  The new **`diverged-box`** check reports two situations:

  - The local and remote sync records disagree *and* the local copy has also
    changed since its own record — both sides moved on independently, so sync
    refuses rather than pick a winner.
  - The local record is complete but the remote one is not: a push from
    another machine died half-way. Nothing could see this before —
    `interrupted-sync` reads only local records.

  Three deliberate constraints, each of which decides whether the check is
  worth reading:

  - **A box that merely needs pulling is not reported.** Its records disagree
    too, so the naive comparison fires on it — and on a fleet where most boxes
    are routinely a sync behind, that would flag hundreds of healthy boxes and
    make the report worthless.
  - **A push still in flight is not reported.** It is indistinguishable from an
    interrupted one, so only a remote record older than six hours counts —
    comfortably longer than any real push here, far shorter than "months".
  - **The local-modification test is the sync engine's own**, same exclude-aware
    scan and same comparison, so `doctor` and `sync` cannot disagree about
    whether a box has changed.

  Cost was the deciding constraint on the design. Fetching every remote record
  takes over two minutes against the storage box and opens enough SFTP
  connections to disturb the syncs running alongside doctor, so the check makes
  **one recursive listing** and reads only the records that listing shows to
  have been written at a different moment than our own — zero extra fetches on
  a healthy machine, measured across 750 records. That prefilter leaves a
  5-second window in which two pushes would look like one; it is documented at
  the constant, and is a far smaller blind spot than the one being closed.

  The check is skipped — reported as `SKIPPED`, never as `ok` — under
  `--no-remote` or when rclone is unresolvable, and a failed listing is a loud
  finding rather than a silent all-clear.

### 🧹 Internal

- The `.rclone_include` / `.rclone_exclude` / `.rclone_filters` filenames are
  now constants in `const` rather than string literals repeated across modules.
- `check_last_time_modified`'s return annotation said `float | None`; it has
  returned a `datetime` since it was written.

## [0.4.6] - 2026-08-23

### 🐛 Bug Fixes

- **Debris that is never synced no longer looks like a local change.**
  `check_last_time_modified` answers "has anything changed here?", and that
  answer drives the sync decision — but it walked every file with no awareness
  of the sync filters. So a file that can never be transferred still marked the
  box as modified: macOS Finder writing a `.DS_Store` was enough to flip a box
  to `NEEDS_PUSH`, and — when the remote had also moved on — to `CONFLICT`.

  Found while diagnosing a box wedged in conflict since March, where 4 of the
  39 "changed" files were `.DS_Store`. This is the mechanism behind boxes
  desyncing across machines purely from operating-system debris.

  The scan now skips what the sync would skip. Two deliberate constraints:

  - The **box's own** effective exclude file is used, not a hardcoded default.
    A `conf/.rclone_exclude` *replaces* the global default, so assuming the
    defaults for a box that overrides them could prune a directory the box
    really does sync — hiding genuine changes. That false negative would be
    worse than the false positive being fixed.
  - Only **literal names** are applied (`node_modules/`, `.DS_Store`); glob
    patterns are deliberately not interpreted. Reimplementing rclone's filter
    language would be a second, subtly different implementation of the thing
    that decides what actually transfers. The gap errs on the safe side: a
    glob-excluded file can still make a box look modified, but nothing that
    *would* be synced is ever skipped.

- `box-status` now resolves the same effective exclude file `sync` does, so it
  reports the state a sync would act on.

## [0.4.5] - 2026-08-23

### 🐛 Bug Fixes

- **Every rename created a duplicate registration on every other machine.**
  `sync_missing_boxmetas` diffed `_ls_remote - _ls_local` on the full
  `{index_name}/boxmeta.toml` path — a purely additive, one-way comparison. But
  an index name is `{box_id}__{name}` and a rename changes only the name half,
  so a renamed box looked like a brand-new one. Nothing ever removed the stale
  pre-rename registration, so every machine *other* than the one that did the
  rename accumulated two registrations for the same box id.

  Found via `doctor`'s `duplicate-box-id` check, which was reporting three
  boxes on each of macbook, macstudio and ideapad — identical on all three, and
  absent on mymain, which had done the renames.

  Reconciliation is now keyed on **box id**. The same id under a different name
  means the box was renamed elsewhere; since the remote is authoritative for
  names, the local registration is renamed to match — exactly what
  `sync-name --direction to_local` does. Ambiguous cases (more than one
  directory for one id on either side) are skipped rather than guessed at.

  Renames now propagate to every machine on the next meta sync, silently and
  correctly, with no duplicate and no manual step.

### 🔧 Changes

- `rename` no longer warns "Remote box not found. Skipping remote rename." when
  a box simply has not been pushed yet. Since v0.4.3 an unreachable remote
  raises rather than reporting absence, so that path now means only "not on the
  remote yet" — and it says so, noting the new name will be used on first push.

- The `duplicate-box-id` doctor hint said "inspect the duplicates and delete or
  re-create one of them", which does not say *which* to delete and whose
  "re-create" advice would mint a new box id. It now explains that this
  normally means a rename on another machine, that the remote's name is
  authoritative, and that the registration to remove is the one the remote does
  not have.

## [0.4.4] - 2026-08-23

### 🐛 Bug Fixes

- **A sync record that no operation could ever clear.** `sync_helper`'s
  `allow_missing_source` branch — used only for the optional `conf` part, which
  "may not exist on either side" — returns early *before* any sync record is
  written. So if a `conf` transfer was interrupted and the part then vanished
  from both sides, the incomplete local record became permanent: no later sync
  could touch it, and `boxyard doctor` reported `interrupted-sync` for that box
  forever.

  Found on macbook, where obako's `conf` carried an incomplete record from a
  pull interrupted in February 2026 while neither the local nor the remote
  `conf` directory existed at all.

  The record is now cleared when **both** sides are absent — an incomplete
  record describing an interrupted transfer between two things that do not
  exist is noise. Only that case is resolved: a missing source with a *present*
  destination means the part was deleted on the other side, which is a real
  divergence and is left alone.

## [0.4.3] - 2026-08-22

### 🐛 Bug Fixes

- **A network blip was reported as "the remote path does not exist".**
  `rclone_lsjson` returned `None`, and `rclone_cat` returned `(False, None)`,
  for *any* non-zero exit. But rclone signals absence with a specific code — 3
  for a missing directory, 4 for a missing file — and everything else is a real
  failure; an unreachable remote is exit 1.

  That conflation is not merely imprecise, it reports a *different* world:
  `scan_and_rebuild_remote_index_cache` persisted an **empty** index after a
  transient SFTP failure, wiping the cache and forcing further (also failing)
  full scans; and `SyncRecord.rclone_read` reported "no remote sync record" when
  it had only failed to read one, which the sync state machine treats as a
  materially different situation. `_doctor.py` already carried a comment working
  around the behaviour.

  Both wrappers now report absence only for exit 3/4 and raise `RcloneFailed`
  otherwise. **This is a behaviour change**: an operation that previously
  degraded silently now fails loudly, which is the point.

- **`rclone_copyto` ignored `dry_run`.** The parameter was accepted and never
  emitted, so a caller asking for a dry run would silently **write**. No call
  site passes `True`, so nothing was broken in practice, but it was a live trap.

## [0.4.2] - 2026-08-22

### 🐛 Bug Fixes

- **Same-named boxes were silently dropped from a group's symlink tree.** When
  two boxes resolved to the same title in a group, the CONFLICT suffix that was
  supposed to disambiguate them never did. Two compounding faults: the threshold
  was `> 1`, but the counter holds how many boxes have *already* taken the
  title, so the second box to want `"foo"` saw `1`, failed the test, and took
  `"foo"` as well; and the increment landed on the *rewritten* key, so once a
  box became `"foo (CONFLICT 2)"` the count for `"foo"` stopped rising and every
  later box computed that same suffix.

  N boxes sharing a title therefore produced only **two** distinct names, and
  symlink creation resolved each collision last-one-wins — so five same-named
  boxes yielded two symlinks and three boxes were simply absent from the group,
  with no warning. Since every `active/*` group uses
  `box_title_mode = "name"`, this meant real work could quietly go missing
  from `~/g`.

  Numbering is now sequential and every box gets its own symlink: `foo`,
  `foo (CONFLICT 1)`, `foo (CONFLICT 2)`, …

## [0.4.1] - 2026-08-22

Two more silent failures, of the same family as the v0.4.0 permissions bug: a
swallowed error producing wrong behaviour instead of an error.

### 🐛 Bug Fixes

- **A box with changes under an unreadable directory was never pushed.**
  `check_last_time_modified` swallowed every `OSError` from `os.scandir`. That
  walk answers "when did this box last change?", and the answer drives the sync
  decision — so those changes lowered nothing, the box reported an older mtime,
  looked `SYNCED`, and was silently left unsynced. It now raises, naming the
  directory to fix. A directory or file that *vanishes* mid-walk is a real race
  and is still tolerated.

- **A remote sync record disappearing mid-pull raised an opaque
  `AttributeError`.** `sync_helper` read the remote record after a successful
  pull and called `.rclone_save` on it without a `None` check, so a box deleted
  from another machine mid-sync produced `'NoneType' object has no attribute
  'rclone_save'`. It now raises `SyncFailed` with an explanation. The local
  record is already incomplete at that point, so the next run sees
  `SYNC_FROM_REMOTE_INCOMPLETE` and can safely retry.

## [0.4.0] - 2026-08-22

Nine bugs, found by building a second implementation and comparing the two.
Most were silent: they produced wrong state rather than an error, which is
exactly the failure mode this codebase's "loud errors, never silent fallbacks"
rule exists to prevent.

### 🐛 Bug Fixes

- **The CLI ignored `BOXYARD_CONFIG_PATH`.** The entrypoint read the `--config`
  flag and otherwise went straight to the default, while the variable was
  consulted only when resolving `rclone_path` and in an `init` message telling
  the user to set it. So `boxyard init --config <path>` instructed you to set a
  variable that every subsequent command then discarded, silently operating on
  the default config. Resolution is now flag, then env var, then default.

- **`boxyard init` ignored the global `--config` too**, keeping its own
  `--config-path` and defaulting independently of it. `--config-path` still
  wins when given, but it now falls back to the same resolution as every other
  command.

- **A shrunken permissions manifest could permanently lose exec bits.**
  `os.walk` swallowed unreadable directories, so every file beneath one was
  silently dropped from `.boxyard-perms.json`; the shrunken manifest was then
  pushed, and every machine that pulled it lost the `+x` bits it no longer
  mentioned. Walk errors now raise. A file vanishing between `readdir` and
  `stat` is still tolerated — that race is real.

- **The permissions manifest could chmod paths outside the box.**
  `apply_exec_manifest` did not reject entries that were absolute or contained
  `..`, and `pathlib` join lets an absolute entry *replace* the root. Manifests
  arrive from a shared remote, so entries are now validated — all of them,
  before any chmod, so a bad manifest cannot half-apply. A corrupt manifest
  raises rather than warning; an absent one remains a legitimate no-op.

- **The manifest listed non-regular files.** `_iter_regular_files` excluded
  only symlinks, but `os.walk` also yields fifos, sockets and devices. rclone
  does not transfer those, so their exec bits were pure noise.

- **A box lock could escape the locks directory.** `box_sync_lock_path` did no
  validation, so an index name containing `/` nested the lock inside a tree and
  one containing `..` placed it outside `~/.boxyard/locks` entirely. Index
  names are now validated as a single path component — deliberately more
  leniently than `validate_box_name`, since box names went unvalidated before
  v0.3.3 and legacy boxes must still be lockable.

- **`cleanup_stale_locks` always returned `[]`.** `filelock`'s own `release()`
  unlinks the lock file, so the following `unlink()` raised `FileNotFoundError`
  — which a blanket `except OSError` swallowed, taking the `removed.append()`
  on the next line with it. Files *were* deleted; callers were told nothing
  had been. `auto_cleanup_stale_locks(verbose=True)` could therefore never
  print anything. Only "the file disappeared underneath us" is tolerated now.

- **A malformed `filter_expr` did not fail at config load.**
  `get_group_filter_func` only tokenizes eagerly and re-parses on every call,
  so `"(a AND b"` compiled fine and raised only when the virtual group was
  first evaluated — during symlink building, far from the typo that caused it.

- **`validate_group_name` accepted a trailing newline.** Python's `$` also
  matches immediately before one, so `"proj\n"` passed and would have been used
  verbatim as a directory name in the group tree. Now uses `re.fullmatch`.

- **`BoxMeta.create` was broken whenever an explicit timestamp was passed** —
  for every input type, a `TypeError` for a datetime and an `AttributeError`
  for a string. Unreachable, because `new_box` builds its `BoxMeta` directly,
  so nothing ever caught it.

### 🔧 Changes

- **Replaced the `toml` dependency with `tomllib` + `tomli_w`.** `toml` 0.10.2
  has been unmaintained since 2020 and silently corrupts control characters:
  dumping `"del\x7fchar"` produced `"delx7fchar"`, round-tripping back to the
  wrong string with no error. `tomllib` is in the standard library on the
  Python versions boxyard supports, which as a side effect makes `_fast.py`
  genuinely dependency-free for the first time.

  Written TOML now formats lists one per line rather than `[ "a", "b",]`. This
  is cosmetic and causes no resync: `boxmeta.toml` is only rewritten when a box
  is modified, and both formats parse.

- `creator_hostname` now rejects control characters. This began as a workaround
  for the `toml` corruption above and is kept on its own merits: such a
  character in a machine name is meaningless and would corrupt the output of
  `list` and `doctor`, which print it.

## [0.3.3] - 2026-08-11

### 🐛 Bug Fixes

- A single bad box name no longer bricks the whole yard. A box's `index_name`
  (`{box_id}__{name}`) is interpolated straight into filesystem paths, so a name
  that was not a single path component spread the box over a nested directory
  tree — `boxyard new -n "/github.com/user/repo"` created
  `local_store/<loc>/<box_id>__/github.com/user/repo/boxmeta.toml`. The top
  level of that tree, `<box_id>__`, then looked like a box registration with no
  `boxmeta.toml`, and since `create_boxyard_meta` scans registrations at depth 1
  and let `BoxMeta.load` raise, *every* later command that refreshed the meta
  died — for all boxes, permanently, until the directory was removed by hand.

  Three independent fixes, each of which alone would have prevented the outage:

  - **Box names are validated.** `validate_box_name` rejects anything that is
    not a single path component (separators, `.`/`..`, a leading dot,
    leading/trailing whitespace, empty, NUL), and `new_box` / `rename_box` call
    it before any of the box exists.
  - **The meta refresh survives an unreadable registration.**
    `create_boxyard_meta` now reports each one it cannot load on stderr,
    pointing at `boxyard doctor`, and builds the meta from the rest instead of
    failing the whole yard.
  - **Box creation is transactional.** Writing the boxmeta, creating the
    directories, cloning or moving in the contents, and `git init` now run under
    one rollback: on failure the box directories are removed (a moved-in
    `from_path` is put back where it came from), the global lock is released,
    and the error is re-raised. Previously a failed `git clone` left a
    fully-registered box behind, so each retry silently added another duplicate.

## [0.3.2] - 2026-07-27

### 🐛 Bug Fixes

- Sync no longer wedges forever when the machine sleeps mid-sync. rclone
  children spawned just before a suspend come back with dead TCP connections
  and can spin at 100% CPU indefinitely — observed as two `lsjson` processes
  burning a core each for 9.5 hours, with no open sockets. Because
  `run_cmd_async` awaited `communicate()` with no timeout, both `multi-sync`
  concurrency slots stayed occupied, the run never returned, and the supervisor
  loop never started another cycle — so *no box synced at all* until the
  processes were killed by hand.

  Two guards now cover this:

  - **A suspend watchdog.** `time.monotonic()` does not advance while the system
    is suspended but `time.time()` does, so a divergence between them means the
    machine slept and every in-flight rclone child is holding a dead
    connection. Those children are killed and the caller fails loudly and
    retries on fresh connections. This deliberately does *not* penalise long
    transfers: a multi-hour push is untouched unless it spanned a suspend, in
    which case it was already doomed.
  - **A timeout on bounded operations.** `lsjson`, `mkdir`, `cat` and
    `path-exists` do inherently finite work, so they now take a wall-clock
    ceiling (`RCLONE_LISTING_TIMEOUT`, 10 min) and raise `CommandTimeout`
    rather than hanging. Transfers stay unbounded, since no wall-clock limit is
    meaningful for them.

  Note that rclone's own `--timeout` (5m IO idle), `--contimeout` (1m) and
  `--sftp-idle-timeout` (1m) were all in effect during the incident and none of
  them fired, which is why the guard has to live on the boxyard side.

- Subprocesses are now spawned in their own session and killed by process
  group, so an rclone that has spawned children cannot leave orphans behind
  when it is timed out or killed.

## [0.3.1] - 2026-07-11

### 🐛 Bug Fixes

- `delete` now also purges the box's sync-record and sync-backup directories
  (local + remote); previously every delete left orphaned sync records that
  `boxyard doctor` flagged (#13).
- `delete` removes the local box *before* creating the tombstone / purging the
  remote, so a permission failure aborts cleanly instead of leaving a partial
  delete; and a file owned by another user now raises an actionable
  `chown` hint instead of a raw traceback (#15).
- `remove-parent --parent-id <id>` can now drop a dangling parent whose box has
  been deleted (the exact fix `boxyard doctor` recommends for `tree-orphans`);
  previously it errored with "Box with id … not found" (#14).

## [0.3.0] - 2026-07-10

### 🚀 Features

- Preserve the executable bit (`+x`) across sync. rclone drops Unix mode on
  transfer and the SFTP backend can't carry mode metadata at all, so `+x` was
  lost on every round-trip. Boxes now carry a `.boxyard-perms.json` manifest at
  the DATA root recording which files are executable; it is (re)generated before
  a push and re-applied after a pull. Always-on. v1 is additive-only (restores
  `+x`, never clears it) so mixed old/new client versions stay safe during
  rollout. New module `boxyard._utils.perms`.

### 🔧 Tooling

- Pin `nblite>=1.2.2` — 1.2.1 emits broken relative imports for function-export
  modules.

## [0.1.8] - 2025-11-19

### 🚀 Features

- Improved the results UI for multi-sync
- Lowered the DEFAULT_MAX_CONCURRENT_RCLONE_OPS
- Added --pick-first option to CLI 'path'
- Removed 'sync_repometas' and replaced it with 'sync_missing_repometas'
- Option --no-print-skipped in multi-sync
- 'create_user_symlinks' now reloads the repoyard_meta file before doing anything
- Include symlinks in rclone_lsjson
- Improved prints
- Changed default NameMatchMode for 'path'
- Optimised 'check_last_time_modified'
- Allow for nested repo groups now in the user repo group symlink creation
- List-groups can now be targeted towards specific repos
- Default repo groups

### 🐛 Bug Fixes

- Had not pushed these. the multi-sync cli with better UI
- Missing dev dependency
- Typo in 'new' cli
- Normalise 'group' flags in cli
- References to non-existing function get_repo_full_name_from_cwd
- 'create_repoyard_meta' was trying to any file in the local store as a repometa (e.g. '.DS_Store')
- Improved the 'get_sync_status' logic
- Typo
- Bugs in 'get_sync_status'
- 'exclude' now checks if the repo is local
- Proper validation on group names and allow for '/' in the group filters

### 🚜 Refactor

- Headline

### ⚙️ Miscellaneous Tasks

- Uv.lock
- Update version in pyproject.toml
## [0.1.7] - 2025-11-16

### 🐛 Bug Fixes

- Typo SyncRecord.creator_hostname -> SyncRecord.syncer_hostname

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Update version in pyproject.toml
## [0.1.6] - 2025-11-16

### 🚀 Features

- The global default.repoyard_exclude file is now used by default if no exclude file is present

### 🐛 Bug Fixes

- Symlinks for fake stores
- Typo in numbering
- Added symlink syncing

### 🚜 Refactor

- Changed '.repoyard_*' files to '.rclone_' files. more fitting

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Update version in pyproject.toml
## [0.1.5] - 2025-11-16

### 🚀 Features

- 'get_sync_status' now raises an exception if there is no local sync record, but there is a remote folder. this indicates that the sync was aborted.
- Safer syncing. syncing now creates an intermediate sync record signifying an ongoing sync, and also backups the synced files temporarily.
- Enabled 'soft interruption', intercepting OS signals to delay termination until it is safe to do so.
- Soft interruption will now force interrupt if 3 signals are sent
- Better in-progress output for multi-sync

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Update version in pyproject.toml
## [0.1.4] - 2025-11-16

### 🚀 Features

- Supports specifying the creation timestamp
- 'create_user_repo_group_symlinks' now removes empty folders that do not correspond to repo groups in the user repo group folder
- Can now pass paths instead of specifying repo names to certain CLI functions

### 🐛 Bug Fixes

- Creation timestamp specification wasn't working
- Typo in 'new'
- Removed some tests now that bisync is no longer supported
- Repo names were wrongly parsed from paths
- Cmds had changed

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Ran nbl prepare with new nblite version
- Update version in pyproject.toml
## [0.1.3] - 2025-11-16

### 🚀 Features

- Implemented new syncing system based on 'rclone sync' rather than 'rclone bisync'
- 'copy_from_path' in 'new_repo'
- RepoMeta.get_user_path
- Fuzzy string matching in repo_name and other modes
- Changed the form of repo ids to a more legible form
- Async support
- 'max_concurrent_rclone_ops' in cli 'sync-meta'
- CLI commands 'list', 'yard-status' and 'multi-sync'
- Enabled support for unique names in repo groups
- Create_user_symlinks
- Changed default
- Cli create-user-symlinks
- 'create_user_repos_symlinks' now removes old symlinks
- User symlink creation does now not delete symlinks that are meant to be there
- CLI commands now creates user symlinks automatically
- Logical expression filtering of groups
- Optimised group filtering

### 🐛 Bug Fixes

- 'new' doesnt reinitialise .git now
- Expanduser issues
- 'get_repo_full_name_from_sub_path' now resolves symlink paths
- Error in checking 'res_dry'
- Removed deprectated 'bisync_helper'
- Properly implemented repo_full_names and storage_locations filter
- Removed dud arguments
- Updated CLIs for the new cmd functions
- 'get_local_sync_record_path' wasn't used in '02_get_repo_sync_status'
- Removed the direction in SyncRecords as it was inconsistent with the approach
- Allow for __init__ in tests/
- Fixed SyncRecord logic
- Typo
- Removed old SyncConfig
- Missing await
- Async_throttler now propagates exceptions
- Create_user_symlinks was async
- Missing import
- Better repo name conflict handling

### 🚜 Refactor

- More sensible default
- Removed dud files
- Fixed import

### 🧪 Testing

- Test.utils
- Finished tests 00 and 01
- Run_cmd
- Test_02_remote
- Fixed test_02_remote

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Added module code so that you can install directly from git
- Uv.lock
- Nbl prepare
- Python-dotenv dev-dependency
- Uv.lock
- Update version in pyproject.toml
## [0.1.2] - 2025-11-14

### 🐛 Bug Fixes

- Was config.json instead of config.toml

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Update version in pyproject.toml
## [0.1.1] - 2025-11-14

### 🐛 Bug Fixes

- Issues with '~' in paths

### ⚙️ Miscellaneous Tasks

- Git-cliff
- Docs defined in nblite.toml
- Update version in pyproject.toml
## [0.1.0] - 2025-11-13

### ⚙️ Miscellaneous Tasks

- Updated nblite
- Updated nblite
- Publish script
- Update version in pyproject.toml

# Go rewrite — current state

Branch: `feat/go-rewrite`. Python `main` is at **v0.4.3**.
Last updated: 2026-08-22 (overnight session).

Design rationale is in [`research/go-rewrite.md`](research/go-rewrite.md);
deliberate divergences and the incident log are in
[`../parity/PARITY-NOTES.md`](../parity/PARITY-NOTES.md).

## The governing rule

**When the implementations disagree because Python has a bug, Python gets
fixed.** Proving the Go implementation correct only means something if the
thing it is compared against is itself correct — and the fixes have to reach
the five machines still running Python anyway.

Twelve bugs found so far this way, all released in v0.4.0–v0.4.3. Every one was
*silent*: it produced wrong state rather than an error.

## Ported and verified

| Package | Source | Verification |
|---|---|---|
| `internal/boxconst` | `const.py` | — |
| `internal/strict` | replaces pydantic `StrictModel` | golden-tested against pydantic bytes |
| `internal/naming` | box/group name rules | ported test suite |
| `internal/config` | `config.py` | probed against live Python; loads the real 200-line config |
| `internal/enums` | `_enums.py` | — |
| `internal/groupexpr` | `_utils/logical_expressions.py` | **131,744 differential comparisons, 0 mismatches** |
| `internal/perms` | `_utils/perms.py` | live differential; byte-compatible manifests |
| `internal/locking` | `_utils/locking.py` | cross-process contention + SIGKILL recovery |
| `internal/models` | `_models.py` (BoxMeta, BoxyardMeta, SyncRecord) | **byte-identical round-trip of the real 583-box metadata** |
| `internal/sysinfo` | `get_hostname` | macOS pretty-name path preserved |
| `internal/syncengine` | `get_sync_status`, `sync_helper` | **2400-scenario exhaustive differential, 0 mismatches** |
| `internal/tombstones` | `_tombstones.py` | byte-compatible, Python-parseable |
| `internal/remoteindex` | `_remote_index.py` | ported test suite |
| `internal/symlinks` | `create_user_box_group_symlinks` | 19 scenarios run through the real Python builder |
| `internal/runner` | `run_cmd_async` + suspend watchdog | separate wall/monotonic seams; mutation-checked |
| `internal/rclone` | `_utils/rclone.py` | all 65 Python argv tests ported + real-rclone round trip |
| `internal/cli` | `_cli/main.py` — **`which`, `list`, `list-groups`, `new`** | byte-identical to Python on the real yard |
| `internal/ownership` | `_ownership.py` | refusals asserted to name the fix and BOTH ways out |
| `internal/cmds` | `cmds/` — **`init`, `new_box`, `get_box_sync_status`, `sync_box`** | isolated test yards; git-URL differential; the four ownership/ordering properties mutation-checked |

`boxyard which -i` on the real 583-box yard: **185 ms → 6.3 ms (29×)**.

The three ported commands cover everything myrig's box picker, the sesh plugin
and the `mcd`/`bx`/`nb` shell functions actually invoke. Flags not yet ported
(`--view tree|groups`, the hierarchy filters) **fail loudly with exit 1** rather
than being ignored.

## Keeping parity as Python moves

Python reached v0.5.1 while this port sat at the v0.4.x line, and checking the
port against it found **three gaps, each of which would have shipped as a silent
data problem**. Re-check on every Python release; do not assume the frozen
differential still covers you, because it captures the behaviour of the day it
was taken.

| gap | what it would have done | fixed in |
|---|---|---|
| `boxmeta.toml` decoded with `DisallowUnknownFields` | once release 2 writes `write_owner`, every box a Go binary touched would vanish from `boxyard_meta.json`, `boxyard list`, `~/g` and `multi-sync` — silently, and not healed by upgrading | `594a646` |
| `config.toml` decoded with `DisallowUnknownFields` | the myrig template adding `machine_name` would break EVERY command on EVERY machine at once — config.toml is one shared artefact | `61b9c42` |
| `LocalLastModified` ignored the sync excludes | pre-v0.4.6 behaviour: a `.DS_Store` alone flips a box to NEEDS_PUSH, and to CONFLICT when the remote has also moved. The mechanism behind boxes wedging on OS debris | `51b3784` |
| CONF's absence read as `EXCLUDED` | self-perpetuating: conf/ is missing, so it is judged excluded, so it is never pulled. A box's rclone filters would exist only on the machine that wrote them, and one with `.rclone_include` would sync EVERYTHING on the second machine | `e6d82c4` |

The fourth was carried over the same DAY Python fixed it (v0.5.3), rather than
being found later as a gap. That is the intended cadence.

**One thing did not translate directly.** Python defaults its flag to `True`
(the DATA meaning) and Go cannot default a bool to true, so a literal
translation would make the ZERO VALUE mean CONF — and a caller who forgot the
field would silently un-exclude a box, pulling data back onto a machine the
user removed it from. The Go field is phrased inversely
(`TreatLocalAbsenceAsNeedsPull`) so the zero value is the safe one. The
existing `TestExcludedWhenOnlyRemoteExists` catches the wrong choice
immediately.

Two of the first three were found by writing the *next* piece and checking its
assumptions against Python, not by a test failing. The port's own suite was
green throughout. The frozen differential does not protect you here: it
captures the behaviour of the day it was taken, and all its scenarios exercise
the DATA meaning — which is exactly why they still passed after the CONF fix.

`sync_box` is the one that ties the stack together, and it is an ORDERING
problem rather than a transport one. Every step encodes a failure that has
actually happened: the local-storage shortcut (v0.5.5), the batched tombstone
probe (v0.5.1), resolving the remote by box id rather than by name, META and
CONF both syncing whenever DATA does, the ownership decision sitting between
META and DATA and re-reading the boxmeta from disk, and the `rclone check`
probe that stops a `.DS_Store` from reading as a real change. The four
properties that matter — a non-owner never pushes, META follows DATA, CONF
follows DATA, and DATA uses the box's OWN exclude file — are each
mutation-checked.

## What porting `new_box` turned up

Nothing wrong on the Go side — but five faults in the Python, all found by
reading it line by line rather than by any test failing. They shipped as
**v0.5.5**, and the corrected behaviour is what the Go port implements.

| fault | why nothing caught it |
|---|---|
| `sync_before_new_box` imported `sync_boxmetas`, a name `boxyard.cmds` has never exported, and drove it through `asyncio.get_event_loop()`, which RAISES on Python 3.14 | the setting defaults to `False`, so the branch had never once run |
| `--creation-timestamp-utc` checked `<now>_<subid>` for collisions and then substituted the caller's timestamp — the id written was never the id checked | the subid space makes a real collision rare; the *guarantee* was void, not the outcome |
| a failing `git init` rolled the whole box back, despite a "Warning: …" branch the code has always carried | `check=True` raised before the warning could run — dead code |
| a failed `git clone` reported only an exit status; `stderr` went to `DEVNULL` | you only notice when a clone fails, and then you blame the URL |
| `boxyard new --no-refresh-user-symlinks` rebuilt the symlinks anyway | `new` was the ONLY command that declared the flag and then ignored it |

Two more of the same shape, fixed alongside: the global lock was released
outside any `try`, and the box-id collision snapshot was read **before** the
lock was taken, which made the lock's guarantee vacuous.

The Go port additionally found that the Python suite spawns a bare `python` for
its subprocess tests — absent on a machine that installs only `python3`, so ten
tests failed locally while CI stayed green (uv provides `python` there). One of
them had therefore never tested what it claims.

**The dev/CI interpreter is not the deployed one.** CI runs 3.11 and 3.12;
`uv tool install` picked **3.14**. The `get_event_loop` fault is exactly that
gap: deprecated in 3.12, raising in 3.14, and green on every version CI tests.

## In progress

Nothing. Wiring is done: `internal/storage` adapts `rclone.Client` to
`syncengine.Prober`/`Storage`, `tombstones.Store` and `remoteindex.Store`, with
compile-time interface assertions so a domain package growing a method fails to
build at the seam rather than at a call site. It holds no decisions — every
method is a translation — so there is nothing in it that can disagree with
Python. Errors are never collapsed into empty results there; that is precisely
the bug fixed in v0.4.3.

## Not started

- Remaining CLI commands (20 of 24; ~213 flags total). The next blocker for
  several of them is `_get_box_index_name` — resolve a box by path / index /
  id / name with match modes — which ~15 commands share. `sync` and
  `box-status` are implemented at the `cmds` layer but not registered, because
  root.go's rule is that a command is added only when it is complete
- `internal/cmds` — the remaining implementations (include, exclude, delete,
  rename, copy, force-push, doctor, modify_boxmeta, sync_missing_boxmetas, …).
  `new_box` refuses loudly on `sync_before_new_box = true`, and `new`'s
  `--group`/`--parent` refuse loudly, all three pending `modify_boxmeta` /
  `sync_missing_boxmetas`
- The ownership COMMANDS (claim, release, steal, discard-local). The read side
  is ported — a Go `sync` that ignored `write_owner` would push from a
  non-owner, which is a data-safety divergence rather than a missing feature
- The two render surfaces: `path`'s Textual TUI and `multi-sync`'s live table.
  **Open question:** `path`'s TUI may be dead weight — the picker actually in
  daily use is `boxyard-pick` (fzf + `boxyard-groups.py` over
  `boxyard list -o json`), not `boxyard path`'s interactive mode.
- Distribution: GoReleaser, and the myrig install switch.

## How to verify what is here

```bash
go test ./...                      # unit tests, no network
go test -tags parity ./parity/     # provisions an isolated remote sandbox
cd /tmp/boxyard-pyfix 2>/dev/null || true   # (worktree may be gone; see below)
uv run pytest -q -m "not integration"       # Python: 750 tests
uv run pytest -q -m integration             # Python: 39 pass / 2 skip
```

The parity suite compares against **this repo's** `.venv/bin/boxyard`, not the
user's installed `~/.local/bin/boxyard` — so it measures against the fixed
Python, and cannot perturb the live supervisor.

## Things a future session should know

- **The Python fixes are merged to local `main` but NOT pushed, and NOT
  deployed.** The five other machines still run the old Python. Rolling out is
  a deliberate step that needs a decision — two of the fixes turn previously
  silent conditions into errors.
- **A worktree at `/tmp/boxyard-pyfix`** holds the Python fix branches. It is
  in `/tmp`, so it will not survive a reboot; the branches are in the repo, so
  nothing is lost, but `git worktree prune` may be needed.
- **Differential testing is what has caught everything that mattered.** Three
  separate real bugs in the Go code were found by comparing against Python over
  a generated input space, and none would have been found by reading the code.
  Design the sample around the *boundaries of the encoding*, not typical
  values — a 1-in-1000 timestamp divergence slipped through a 10-case
  differential and was caught only by a sample built to hit second boundaries.
- **The user's supervisor runs `boxyard multi-sync` continuously** on this
  machine. Never take the real global lock, and never point a test at
  `~/.boxyard`, `~/dev` or `~/g`.

## Open questions for Lukas

1. Roll the v0.4.x Python fixes out to the fleet now, or wait?
2. Does `boxyard path`'s Textual TUI need porting at all?
3. Replace `lukastk/boxyard` in place at v1.0.0, or a new repo for the Go one?
4. `_remove_empty_non_group_folders` compares group NAMES against directories
   named after `symlink_name`, so group directories with a `symlink_name` are
   pruned when empty while others are kept. Fixing it would leave ~30
   permanently empty directories in `~/g`, so it was left alone — your call.

# Research: rewriting boxyard from Python to Go

Date: 2026-08-22. Box: `20260822_tsl6xn__boxyard-go`.

> **Status: DIRECTION AGREED, NOT STARTED.** Full rewrite to a single static Go
> binary. This document is the feasibility assessment and the migration plan.
> Nothing has been ported yet.

## TL;DR

- **Feasible, and unusually clean for a rewrite**, because boxyard's entire
  external contract is *(a)* the CLI and *(b)* the on-disk file formats. A sweep
  of `~/mysetup` found **zero `import boxyard`** outside boxyard's own tests.
  mysystem talks to it by shelling out and by reading `boxyard_meta.json`
  directly (in TypeScript); sesh's plugin runs `boxyard list -o json`; myrig
  drives the CLI from zsh. Nothing depends on the Python API.
- **rclone does all the hard I/O.** There is no S3/SFTP client to reimplement —
  `_utils/rclone.py` is an argv builder plus stdout parsing over ~12 rclone
  subcommands. rclone stays an external binary dependency after the rewrite.
- **The subtle logic is pure and over-tested.** `get_sync_status` (the
  conflict / interrupted-sync state machine), the group filter-expression
  parser, box-id parsing, the perms manifest, symlink materialisation — all
  pure functions, behind 621 unit tests that pass today. They port
  near-mechanically and the tests become Go table tests.
- **The real risk is the migration window, not the code.** Six machines sync to
  shared remotes holding 583 boxes. Any period where some machines run Python
  boxyard and others run Go is a period where two implementations write sync
  records to the same remote. The whole plan below is shaped around collapsing
  that window and proving equivalence before entering it.
- **The single biggest technical hazard is a strictness inversion** (see below):
  pydantic's `extra="forbid"` + required fields are load-bearing here, and Go's
  decoding defaults are the exact opposite. This must be designed for, not
  discovered.
- Rough effort: **17–28 focused days**, plus a **~2 week shadow-run soak** that
  cannot be compressed.

## Why (the decision)

Locked in 2026-08-22:

1. **A single static binary.** No Python runtime in boxyard's dependency chain
   on any of the six machines. This is the primary driver.
2. **Go is a generally safer language** than Python for this kind of tool.
3. **One `boxyard` binary, not a binary plus a separate `BoxyardFast` library.**

Point 3 deserves spelling out, because it is the clearest structural win.
`_fast.py` (274 LOC) exists solely so that latency-sensitive callers can read
boxyard metadata *without paying pydantic/typer import cost* — it is a
hand-maintained, dependency-free duplicate of the meta reader, policed by a unit
test that greps its own source for `import boxyard`
(`src/tests/unit/test_fast.py:60-65`). Its consumer, `boxyard-shell-helper`, is
a second console entry point in `pyproject.toml` bound to `^G` in myrig.

In Go there is no import cost, so **the entire reason for `BoxyardFast` evaporates.**
`_fast.py` + `_shell_helper.py` + that isolation test all collapse into ordinary
code paths in one binary.

**What does *not* go away:** `boxyard_meta.json` remains a **public format
contract**, because two other consumers read it directly and are unaffected by
the rewrite:

- `mysystem/mysystem/src/boxyard.ts` — `BoxyardService`, a cached TS reader
- `myrig/home/.myrig/scripts/boxyard-groups.py` — reads `boxyard list -o json`

Consolidating to one binary removes the *Python duplicate*, not the format's
public status. The format spec still has to be written down and honoured.

### Measured benefit (secondary, but real)

```
boxyard --help          0.18s
boxyard list            0.18s   (583 boxes)  ->  ~5ms in Go
boxyard-shell-helper    0.086s  (^G widget)  ->  ~3ms in Go
```

`boxyard list` is on the hot path of `mcd` / `bx` / `boxyard-pick`, and the
shell helper fires on every `^G`.

## Scope

| | |
|---|---|
| Source | 9.4k LOC generated (`src/boxyard/`), 12.5k LOC in `pts/` |
| Tests | 12.3k LOC — 621 unit + 41 integration (39 pass, 2 skip, ~2 min) |
| CLI | 24 commands, **213 `Option(...)` declarations**, 2 entry points |
| Runtime deps | filelock, pydantic, python-ulid, rich, textual, toml, typer |
| Largest units | `_cli/main.py` 2735, `_models.py` 870, `_utils/rclone.py` 569, `cmds/_doctor.py` 518 |
| Activity | 157 commits Nov 2025, **~27 in the last 5 months**. No open issues, no in-flight branch |

Development has gone quiet and there is no competing work in flight, so this is
not a moving target.

## The strictness inversion — the main technical hazard

Eight models inherit `const.StrictModel` (`extra="forbid"`), with required
fields and `@model_validator` hooks: `Config`, `StorageConfig`, `BoxGroupConfig`,
`VirtualBoxGroupConfig`, `BoxMeta`, `BoxyardMeta`, `SyncRecord`, `Tombstone`.

This strictness is **load-bearing**. A malformed `boxmeta.toml` or a sync record
with an unexpected key fails loudly today, which is exactly what `AGENTS.md`
demands ("ALWAYS prefer loud errors and exceptions over silent failures"; "no
defensive fallback values"). `boxyard doctor`'s `broken-registration` check
depends on parse failure being detectable.

**Go's defaults invert every part of this.** `encoding/json` and every TOML
library silently ignore unknown keys, and a missing `storage_location` decodes
to `""` rather than erroring — a zero value that will propagate into a remote
path and fail somewhere far from the cause. A naive port would quietly convert
boxyard's loudest safety property into its silent one.

**Design constraint for the port:** every model load goes through a strict
decode — `DisallowUnknownFields` (and the TOML equivalent) plus an explicit
`Validate() error` per type asserting required fields are non-zero — mirroring
`StrictModel` deliberately. This is a `StrictModel` equivalent written once and
used everywhere, not per-call-site checks. Treat it as Phase 1's first task.

### Where "Go is safer" actually pays here

- **Pays off:** exhaustive `switch` over `SyncCondition` in the sync state
  machine and `sync_helper`'s unsafe-condition branching — a missing case is
  reviewable and lintable (`exhaustive`), where Python falls through silently.
  Explicit `error` returns replace the current mix of typed exceptions and bare
  `raise Exception(...)`. Compile-time enforcement across a 213-option CLI.
- **Does not pay off automatically:** nil-map and nil-pointer panics are Go's
  version of `AttributeError`, and zero values *hide* the class of bug pydantic
  currently surfaces. Hence the constraint above.

## Other things that must not be missed

- **macOS pretty hostname.** `_utils/base.py:47` returns
  `scutil --get ComputerName` on Darwin ("Lukas's MacBook Pro"), not
  `platform.node()`. This value is persisted into `boxmeta.toml.creator_hostname`
  and every sync record's `syncer_hostname`. Must be replicated exactly.
- **Timestamp encoding.** pydantic writes 6-digit microseconds with a `Z`
  suffix: `2026-06-01T11:09:00.415000Z`. Go's default RFC3339 formatting drops
  trailing zeros. Needs an explicit layout.
- **Legacy id formats.** `creation_timestamp_utc` appears both as `20260601`
  (date-only) and `20250622_000000` (legacy date+time), and subids appear in
  mixed case (`aTrMF` vs `rh9q4r`). Parsing must stay permissive; `doctor`'s
  `malformed-name` check encodes the accepted set.
- **TOML rewrite churn.** Python's `toml` writes arrays as `[ "a", "b",]`. Go
  libraries write `["a", "b"]`. Harmless semantically, but a Go boxyard
  rewriting `boxmeta.toml` will mark every box's META part dirty and push it.
  Either match the formatting or accept a one-time fleet-wide meta resync.
- **The stdout / exit-code contract.** `mcd` does `cd $(boxyard path ...)`;
  `boxyard which -j | jq -r '.box_id'`; `nb` consumes `boxyard new`'s printed
  index name; `doctor` exits 1 on findings under supervisor. One stray log line
  on stdout breaks a shell function silently.
- **Typer flag shapes.** Boolean options render as
  `--refresh-user-symlinks/--no-refresh-user-symlinks` pairs; `--group` is
  repeatable; enums are value-matched. Cobra can do all of it, but the `--x/--no-x`
  pair is not its default and needs deliberate handling.
- **`boxyard-shell-helper` becomes a subcommand.** Requires editing
  `myrig/home/.myrig/zshrc/boxyard_completion.sh:17`. Suggest a hidden
  `boxyard shell-helper search` preserving the exact `display\trelative_path`
  output.
- **Losing the nblite / `pts` workflow.** Go has no equivalent. Arguably a
  simplification given `nbs/` was already dropped for git friction, but it is a
  real change to how this repo is developed, and `AGENTS.md` + `nblite.toml` go
  with it.

## The two render surfaces

`_cli/path_tui.py` (Textual tree + live filter, 176 LOC) and `multi-sync`'s
`rich.Live` progress table (275 LOC) are the least mechanical work — budget 2–3x
the Python LOC in bubbletea/lipgloss.

**Open question worth settling before porting `path`'s TUI at all:** the picker
actually in daily use is `boxyard-pick` (fzf + `boxyard-groups.py` over
`boxyard list -o json`), not `boxyard path`'s interactive mode. If the Textual
TUI is dead weight, dropping it removes the larger of the two surfaces.

## Distribution

- `CGO_ENABLED=0` throughout. boxyard uses `platform.node()` / `scutil` rather
  than `os/user`, so a pure-Go static build should be clean.
- GoReleaser -> GitHub Releases: `darwin/arm64`, `darwin/amd64`, `linux/amd64`,
  `linux/arm64`, `android/arm64` (termux).
- `myrig/setup/installs/all/python_tools.py:14-21` changes from
  `uv tool install --upgrade git+...` to a release-binary fetch — boxyard leaves
  the "python tools" install phase entirely. This *improves* bootstrap: a fresh
  machine no longer needs uv/Python before it can sync boxes.
- `myrig/scripts/installs/termux/termux.sh:46` (`pip install git+...`) becomes
  the same fetch. Termux is the fiddliest target.
- rclone remains external, already installed by `setup/installs/all/system_utils.py:13-16`.
- **Repo/versioning decision needed:** replace `lukastk/boxyard` in place (keeps
  history, issues, tag lineage; go `v1.0.0`) versus a new repo. In-place means
  the PyPI package goes stale — suggest keeping PyPI publishing until cutover,
  then retiring `release.yml`'s publish job and making GitHub Releases the
  distribution channel.

## Plan

### Phase 0 — Freeze the contract (in the Python repo, before any Go)

- Write the format spec — `boxmeta.toml`, sync record, `boxyard_meta.json`,
  tombstone, `.boxyard-perms.json`, remote directory layout — with golden
  fixture files.
- **Refactor the 41 integration tests to drive the CLI instead of the Python
  API.** They already assert mostly on on-disk state; parameterise the binary
  via `$BOXYARD_BIN`. This turns them into an implementation-agnostic
  conformance suite runnable against either implementation.
- Golden-file every command's `--help` and every `-o json` output.

This phase pays for itself even if the rewrite is abandoned.

### Phase 1 — Pure core (~2.5k LOC)

Strict-decode layer first (see above), then config, models, box-id/index-name,
group filter expressions, perms, the sync-status state machine, tombstones,
remote index, and the former `_fast` read path folded in as ordinary code. Port
the 621 unit tests as Go table tests. No commands yet.

This phase is the honest test of whether the port is as mechanical as it looks.

### Phase 2 — rclone layer + sync engine

Argv builder, output parsing, `run_cmd` with process-group kill
(`SysProcAttr{Setpgid:true}` + `syscall.Kill(-pgid, ...)`), the suspend watchdog
(wall vs monotonic delta — in Go, strip the monotonic reading with `.Round(0)`
to get the wall delta), flock locking, `sync_helper`. Conformance suite green
against local-remote fixtures.

### Phase 3 — Commands, in risk order

1. Read-only: `path`, `which`, `list`, `list-groups`, shell-helper — highest
   daily use, biggest latency win, **zero data risk**
2. `init`, `new`, `add-to-group`, `remove-from-group`, `create-user-symlinks`
3. `sync`, `multi-sync`, `sync-missing-meta`
4. `include`, `exclude`, `delete`, `rename`, `sync-name`, `copy`, `force-push`
5. `doctor`, `tree`, `box-status`, `yard-status`, `add-parent`, `remove-parent`

### Phase 4 — Shadow-run

Install as `boxyard-go` alongside Python on one machine (ideapad or pocket4).
Diff read-only output between the two. **`boxyard doctor` is the best
cross-validation tool available** — read-only by construction, structured
`-o json` output, so run both and diff the reports.

Then let Go own sync on that one machine while the other five stay on Python.
That *is* the mixed-fleet test, on live data, with a working fallback one
command away. Soak ~2 weeks; this is wall-clock and should not be shortened.

### Phase 5 — Cutover

Swap the myrig install step; roll all six machines **in one pass, not a
trickle**. Keep Python installed as `boxyard-py` for a rollback window, with a
`TODO(cleanup)` breadcrumb carrying a dated removal condition at every site.

## Library picks

| Need | Choice |
|---|---|
| CLI | `spf13/cobra` (subcommands, completions, `--x/--no-x`) |
| TOML | `pelletier/go-toml/v2` (read + write) |
| ULID | `oklog/ulid/v2` |
| flock | `gofrs/flock` |
| TUI | `charmbracelet/bubbletea` + `lipgloss` |
| fzf | stays a subprocess |

## Effort

| Phase | Estimate |
|---|---|
| 0 — contract + conformance harness | 2–3 days |
| 1 — pure core + test port | 3–5 days |
| 2 — rclone + sync engine + locking | 3–5 days |
| 3 — 24 commands / 213 options | 5–8 days (bulk, mostly mechanical) |
| TUIs (path + multi-sync live) | 2–4 days |
| 4/5 — shadow-run, cutover, myrig/CI/release | 2–3 days + **~2 week soak** |

**17–28 focused days**, compressible with agent assistance since the codebase is
regular and the test suite is a strong oracle — except the soak.

## Open questions

1. Does `boxyard path`'s Textual TUI need porting, or is `boxyard-pick` the only
   picker that matters?
2. Replace `lukastk/boxyard` in place at `v1.0.0`, or start a new repo?
3. Match Python's TOML array formatting, or accept a one-time fleet-wide META
   resync at cutover?

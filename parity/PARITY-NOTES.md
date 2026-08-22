# Known divergences from the Python implementation

Every entry here is a place where the Go implementation deliberately does NOT
match Python, with the reason. Anything not listed here is a bug.

The default is parity, even where Python's behaviour looks wrong. The reason is
the migration window: for a period, some of the six machines run Python boxyard
and some run Go, all writing to the same remote. A Go implementation that is
*stricter* than Python will reject state the other five machines happily
produce, which is a worse failure than reproducing a latent quirk.

---

## 1. Group-expression recursion depth

**Go:** parsing fails with `expression nested too deeply (limit 1000)`.
**Python:** raises `RecursionError` at CPython's default limit, also 1000.

Both refuse, at the same depth, with a catchable error. The explicit limit
exists because unbounded recursion in Go is an *unrecoverable stack overflow*
that takes the process down, where Python's is catchable. The deepest
expression in the live config nests 2 levels.

This is a loud error, not a fallback.

## 2. Group-name regex and trailing newlines

**Go:** `ValidateGroupName("abc\n")` is rejected.
**Python:** accepted.

Python's `re.match(r"^[A-Za-z0-9_\-/]+$", ...)` uses `$`, which matches *before
a trailing newline*. Go's `$` matches end-of-text only.

Reproducing this would mean deliberately accepting a group name containing a
newline, which would then be written into a directory name. Reaching it
requires a TOML quoted key like `"proj\n"`. Judged unreachable in practice and
not worth preserving; recorded here so the difference is discoverable rather
than folklore.

## 3. Group-expression identifier characters (NOT a divergence — a preserved quirk)

`-` and `/` are identifier characters, so `not-archived` tokenizes as a single
group name, not as a negation of `archived`. Likewise `a AND-b` is `[a, "AND-b"]`
and fails as an unexpected token.

This is a genuine footgun for anyone writing `not-archived` and expecting
negation, but it is the existing behaviour and is preserved exactly, pinned by
tests. Listed here so it is not "fixed" by accident.

## 4. Unicode tables

`isIdentifierChar` matches Python's `str.isalnum()` everywhere except
U+2EBF0–U+2EE5D (CJK Extension I), which is Unicode-table version skew (Go 1.24
ships Unicode 15.0.0, CPython 15.1.0) rather than a logic difference. Not
reachable by any real group name.

---

## Resolved — checked, and NOT divergent

### Virtual-group `filter_expr` compile errors

Python's `get_config()` **accepts** a config whose `filter_expr` is present but
unparseable; the `ValueError` surfaces later, from `is_in_group`. An early
version of the Go config failed at load, which was stricter.

Corrected: the Go config compiles the expression at load but *carries* the
failure, surfacing it from `IsInGroup` at the same moment Python surfaces it.
A *missing* `filter_expr` remains a load error, because Python requires the
field.

Worth keeping in mind as a pattern: "compile early, report at Python's moment"
gets the diagnostics of eager validation without the parity break.

---

## 5. `BOXYARD_CONFIG_PATH` (Go honours it; Python ignores it)

**Go:** `config.Load` honours `BOXYARD_CONFIG_PATH`.
**Python:** the CLI ignores it entirely.

This is a **latent bug in the Python implementation**, found the hard way — see
the incident note below.

`src/boxyard/_cli/main.py`'s `entrypoint` callback sets
`app_state["config_path"]` from the `--config` flag, falling back to
`const.DEFAULT_CONFIG_PATH` unconditionally. The env var is read in only two
places, neither of which affects which config the CLI loads:

- `_utils/rclone.py:42`, to find the `rclone_path` key;
- `cmds/_init_boxyard.py:26`, in a *message* telling the user to set it.

So `boxyard init --config <path>` instructs you to set `BOXYARD_CONFIG_PATH`,
and the CLI then silently ignores it and operates on the default config.

Nothing in the rig currently sets the variable (`mysystem-shell/lib/helpers.sh`
reads it, with a fallback, only to locate the config for its own direct
parsing), so honouring it in Go is currently unobservable. **But this is a
mixed-fleet hazard the moment anyone follows `init`'s advice**: Go would use
the named config while the five Python machines used the default one.

The right resolution is to fix Python so both honour it, rather than to
preserve the bug in Go. Flagged for a decision.

---

# Incident: a parity run created a real box in ~/dev (2026-08-22)

Recorded here because the fix shaped the harness design.

**What happened.** The first end-to-end smoke test pointed the Python boxyard at
a sandbox config via `BOXYARD_CONFIG_PATH`. Because the CLI ignores that
variable (above), it used the real config: `boxyard list` returned all 583 real
boxes, and `boxyard new` created a real box, `20260822_ql2jkb__parity-probe`, in
`~/dev`, in `~/.boxyard/local_store`, in `boxyard_meta.json`, and as two group
symlinks under `~/g`.

**Blast radius.** Local only. The test aborted before its `sync` step, so
nothing reached the real remote, and no existing box was touched. The box was
empty apart from an auto-`git init`.

**Detection.** The canary — not the guard. `AssertSandboxed` passed, correctly:
the sandbox *was* well-formed. The canary caught it afterwards by noticing
`~/dev` had gained an entry.

**Cleanup.** The box was removed directly rather than with `boxyard delete`,
because `delete` writes a tombstone to the real remote and that would have left
permanent litter for a box that was never there. `refresh_boxyard_meta` +
`create-user-symlinks` restored the yard; `doctor` reported `stale-cache: ok`
and the entry count returned to its baseline.

**The lesson, and the fix.** *A guard that only inspects configuration cannot
catch a binary that never reads it.* The guard proved the sandbox was
well-formed; nothing proved the process used it.

So the harness gained a second gate, `VerifyIsolation`, which must pass before
`Run` will execute anything at all. It runs a read-only probe and checks both
directions: positively, that a fresh sandbox lists zero boxes; and negatively —
the half that matters — that the output contains none of the real yard's box
names, sampled live from the real metadata. Invocations now also pass `--config`
explicitly, which is the only mechanism that actually redirects the Python CLI.

The canary was hardened at the same time. It had compared mtime and directory
*counts*, which false-positived constantly because the user's supervisor
rewrites `boxyard_meta.json` with byte-identical content every 20 minutes. It
now compares content hashes and directory *entry sets*, and names what appeared
or vanished — "a box named parity-probe appeared in ~/dev" rather than "count
went 117 to 118". A canary that cries wolf is worse than none.

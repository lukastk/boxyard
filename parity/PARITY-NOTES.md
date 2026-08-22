# Parity notes

## Governing principle

**When the two implementations disagree because Python has a bug, we fix
Python — we do not reproduce the bug in Go.**

The point of the parity suite is to prove the Go implementation is correct. That
only means something if the thing it is compared against is itself correct.
Reproducing Python's bugs bug-for-bug would produce a Go implementation that
passes every parity test and is still wrong, and would leave the five machines
still running Python wrong too.

So a discovered bug produces work in *both* trees: a fix in Python (branch
`fix/parity-bugs`, released and rolled out to the fleet) and the correct
behaviour in Go. Parity is then asserted against the fixed Python.

Two consequences worth stating:

- **Python fixes must reach the fleet before, or with, the Go cutover.** A fix
  that lives only in the Go implementation reintroduces exactly the mixed-fleet
  asymmetry this principle is meant to avoid: five machines behaving one way and
  one behaving another.
- **Not every difference is a bug.** Where Python's behaviour is defensible, or
  where the difference is environmental, parity wins and the difference is
  recorded below rather than "fixed".

---

## Remaining divergences (deliberate, not bugs)

### 1. Group-expression recursion depth

**Go:** parsing fails with `expression nested too deeply (limit 1000)`.
**Python:** raises `RecursionError` at CPython's default limit, also 1000.

Both refuse, at the same depth, with a catchable error. The explicit limit
exists because unbounded recursion in Go is an *unrecoverable stack overflow*
that takes the process down, where Python's is catchable. The deepest expression
in the live config nests 2 levels. This is a loud error, not a fallback.

### 2. Unicode tables

`isIdentifierChar` matches Python's `str.isalnum()` everywhere except
U+2EBF0–U+2EE5D (CJK Extension I) — Unicode-table version skew (Go 1.24 ships
Unicode 15.0.0, CPython 15.1.0), not a logic difference. Unreachable by any real
group name.

### 3. `not-archived` is one identifier, not a negation

`-` and `/` are identifier characters in the filter grammar, so `not-archived`
tokenizes as a single group name rather than a negation of `archived`. Likewise
`a AND-b` is `[a, "AND-b"]` and fails as an unexpected token.

**Not fixed, because it is not fixable without breaking group names.** Group
names legitimately contain both characters (`adu-me`, `ctx/macbook`), so the
tokenizer cannot treat `-` as a boundary. The construction is genuinely
ambiguous and resolving it toward "identifier" is defensible; `NOT archived`
with a space works as expected. Recorded so it is not "fixed" by accident.

---

## Fixed in Python (branch `fix/parity-bugs`)

### The CLI ignored `BOXYARD_CONFIG_PATH`

`_cli/main.py`'s `entrypoint` set `app_state["config_path"]` from the `--config`
flag, falling back to `const.DEFAULT_CONFIG_PATH` unconditionally. The variable
was read in only two places, neither of which chose the config the CLI loaded:
`_utils/rclone.py` (to resolve `rclone_path`) and a *message* in `init` telling
the user to set it.

So `boxyard init --config <path>` instructed you to set a variable the CLI then
silently ignored, and every command operated on the default config instead.

**Found the hard way** — see the incident below. Fixed to the conventional
precedence: `--config` flag, then `BOXYARD_CONFIG_PATH`, then the default.

### `validate_group_name` accepted a trailing newline

`re.match(r"^[A-Za-z0-9_\-/]+$", name)` — Python's `$` matches at end of string
*or immediately before a trailing newline*, so `"proj\n"` was accepted and would
be used verbatim as a directory name in the group symlink tree. Fixed to
`re.fullmatch`.

### A malformed `filter_expr` did not fail at config load

`get_group_filter_func` only tokenizes eagerly and re-parses the token stream on
every call, so a structural error such as `"(a AND b"` compiled fine and raised
only when the predicate was first invoked — during symlink building, far from
the config typo that caused it. `VirtualBoxGroupConfig` now validates in a
`model_validator`, so both implementations reject it at load.

---

# Incident: a parity run created a real box in ~/dev (2026-08-22)

Recorded because the fix shaped the harness design.

**What happened.** The first end-to-end smoke test pointed the Python boxyard at
a sandbox config via `BOXYARD_CONFIG_PATH`. Because the CLI ignored that
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
explicitly.

The canary was hardened at the same time. It had compared mtime and directory
*counts*, which false-positived constantly because the user's supervisor
rewrites `boxyard_meta.json` with byte-identical content every 20 minutes. It
now compares content hashes and directory *entry sets*, and names what appeared
or vanished — "a box named parity-probe appeared in ~/dev" rather than "count
went 117 to 118". A canary that cries wolf is worse than none.

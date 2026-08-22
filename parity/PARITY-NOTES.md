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

# Parity suite

Cross-implementation tests proving the Go boxyard behaves identically to the
Python one.

**This directory is temporary.** It exists for the duration of the rewrite and
is deleted at cutover, once the Go implementation is trusted. It is deliberately
kept out of `internal/` so removing it is a single `rm -rf parity/` with nothing
to untangle. Nothing in `internal/` or `cmd/` may import it.

## Safety

The suite runs real boxyard commands against the real SFTP storage box. The
user's actual boxyard — 583 boxes in `~/dev`, backed by `hetzner-box:boxyard` —
must never be touched. There are four independent layers:

1. **`AssertSandboxed`** (`safety.go`) refuses to run unless it can prove the
   target is isolated. Its no-go list is derived at check time from the user's
   live `config.toml`, so it keeps working if they move `~/dev` or add a
   storage location. Every check fails closed.
2. **A dedicated remote prefix.** All remote writes go under
   `boxyard-gotest/`, a *sibling* of the real `boxyard/`, never a child.
3. **An rclone `alias` remote** rooted at `boxyard-gotest`. This is the only
   layer that protects against a path-construction bug *inside boxyard itself* —
   the guard validates configuration inputs, but cannot see paths built at
   runtime. Because the alias's root is the test prefix, every path boxyard
   hands to rclone resolves beneath it.
4. **A canary.** The real yard's observable state is stamped before a run and
   verified byte-identical afterwards. A canary failure is an incident, not a
   test failure.

The guard is itself tested (`safety_test.go`), including by mutation: weaken a
guard clause and a test must fail.

## Running

```bash
go test ./parity/                     # guard unit tests only — always safe, no network
go test -tags parity ./parity/        # full suite: provisions a remote sandbox
```

Each run gets its own `boxyard-gotest/run-<random>/` prefix, so concurrent and
repeated runs never share state, and teardown purges only that subdirectory.

## Layout

| File | Purpose |
|---|---|
| `safety.go` | The guard and canary. Fails closed. |
| `safety_test.go` | Tests of the guard. Always run. |
| `sandbox.go` | Builds and tears down an isolated boxyard + remote prefix. |
| `PARITY-NOTES.md` | Known, deliberate divergences from Python. |

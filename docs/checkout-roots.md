# Checkout roots: local DATA placement

## Three independent coordinates

A Boxyard is one catalog, not one directory and not one remote. Each box has:

1. **catalog identity** — `box_id`, name, groups, parents and write ownership;
2. **remote storage location** — the rclone/local store used for shared DATA,
   META, CONF and sync records;
3. **machine-local checkout root** — the directory in which this machine keeps
   the box's DATA.

The first two are represented by the registration and synced `boxmeta.toml`.
The third is intentionally machine-local. A box can therefore be in root
`volume` on one machine, root `default` on another, and excluded on a third
without changing its identity, remote metadata or sync history.

`local_store` retains its existing meaning: internal registration/META/CONF
state beneath `boxyard_data_path`. The public and internal term for DATA
placement is **checkout root**; it does not overload `local_store`.

## Configuration and legacy schema

`user_boxes_path` is permanently the checkout root named `default`:

```toml
user_boxes_path = "~/boxes"

[checkout_roots.bulk]
path = "~/large-boxes"
```

This is the permanent schema, not a temporary compatibility shim. A legacy
single-root config is already a valid one-root config and needs no rewrite.
The name `default` is reserved and cannot also appear in `checkout_roots`.
Root names use `[A-Za-z0-9_-]+`; root paths must be unique and non-overlapping.
`doctor` reports duplicate/overlapping paths, and checkout-placement mutations
(`new`, `include`, and `relocate`) refuse until they are fixed.

A root can be guarded by an exact mount target and stable filesystem UUID:

```toml
[checkout_roots.volume]
path = "/mnt/data/boxyard"
mount_target = "/mnt/data"
filesystem_uuid = "01234567-89ab-cdef-0123-456789abcdef"
```

`mount_target` and `filesystem_uuid` must be configured together, and `path`
must be that target or a descendant. On Linux, Boxyard reads
`/proc/self/mountinfo`, requires `mount_target` to be an exact mounted target,
and compares its source device to `/dev/disk/by-uuid/<uuid>` (or an explicit
`UUID=<uuid>` source). Directory existence is never treated as proof that the
expected filesystem is mounted. An unavailable/wrong guarded root is never
created and never falls back to `default`.

Unguarded roots are an explicit policy declaration that an ordinary local
path may be created; their availability is declared by configuration rather
than inferred from pre-existing directory contents.

## Placement records

Machine-local state is stored at:

```text
<boxyard_data_path>/placements/<box_id>.json
```

Records are keyed by stable `box_id`, so a rename does not rewrite placement.
Schema v1 records:

- `checkout_root`: preferred/authoritative named root;
- `state`: `included`, `excluded`, or `relocating`;
- `relocation`: source, destination, staging name and durable phase while a
  relocation is in progress.

Writes use a same-directory temporary file, `fsync`, and `os.replace`.
Placement files are never synced and never written into `boxmeta.toml`.

The schema is sparse by design. A missing record has permanent legacy meaning:
preferred root `default`; DATA present at `user_boxes_path/<index_name>` means
included, otherwise excluded. Every placement-changing mutation writes an
explicit v1 record. Excluding changes state to `excluded` but preserves the
root; including without a flag reuses that preference, while
`--checkout-root` overrides it.

Local checkout states exposed by list/status APIs are:

- `included` — complete directory in the recorded available root;
- `excluded` — deliberately absent, with preference retained;
- `unavailable` — recorded as included/relocating, but its guarded root cannot
  be verified;
- `missing` — recorded as included in an available root, but the directory is
  absent;
- `relocating` — a complete authoritative copy exists, but the durable local
  transaction still needs completion.

`unavailable` is never collapsed into `excluded` or `missing`.

## Relocation transaction

`boxyard relocate -r BOX --checkout-root ROOT` is local-only. It never calls
rclone and does not modify remote DATA, remote META or synced box metadata.
It holds the box's operation lock (the same lock used by sync/include/exclude/
delete/rename) and the global state lock.

Before mutation it validates registration, included state, source root,
destination root availability and mount identity, destination collision, and
root configuration.

On one filesystem, DATA moves with `os.replace`, giving an atomic directory
rename. Across filesystems, Boxyard copies to a hidden staging directory using
native filesystem APIs. The copier preserves directories, mode bits,
timestamps, symlinks, sparse extents, and hardlink topology when the destination
supports hardlinks. It rejects sockets, devices and FIFOs rather than silently
changing their meaning. A complete manifest compares paths, types, modes,
timestamps, sizes and SHA-256 file contents before staging is promoted.

Durable phases are:

1. `copying` (cross-filesystem) or `moving` (same-filesystem);
2. `destination_ready` after cross-filesystem verification;
3. `committed` after the destination becomes authoritative.

The placement remains `relocating` through derived group-link replacement and
source deletion. Group links are replaced individually with temporary symlinks
and `os.replace`. The source is deleted only after destination verification,
a durable authoritative-placement commit, and link rebuilding. The final
record then becomes `included` in the destination.

Every interruption leaves either the original authoritative complete copy, or
a committed complete destination (often both). `doctor` reports
`interrupted-relocation`; rerun `boxyard relocate -r BOX` without a destination
to resume the recorded transaction idempotently. A different destination is
refused until recovery completes.

## Authoritative paths for clients

Clients must not reconstruct `<user_boxes_path>/<index_name>`. Use:

```bash
boxyard path -r BOX
boxyard which --path PATH --json
boxyard list --output-format json
boxyard box-status -r BOX --output-format json
```

JSON list records include `checkout_root`, `local_path`, `state`, and
`root_available`; `which` includes `checkout_root`, `local_data_path`,
`checkout_state`, and `root_available`. `BoxyardFast.from_file()` joins the
catalog cache, config roots and local placement records and returns the same
authoritative path. `which` resolves symlinks and searches every configured
root.

## Health checks

`doctor` scans every available root and does not descend into an unavailable
guarded root. It reports root configuration/availability, unknown or malformed
placements, included-but-missing DATA, excluded-but-present DATA, duplicate
physical copies, interrupted relocation, orphan placement records, and
unregistered/malformed entries in every available root. An unavailable root
and the boxes assigned to it remain visible in the catalog and status output.

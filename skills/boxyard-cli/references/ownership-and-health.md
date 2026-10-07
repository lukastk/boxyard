# Write ownership and health checks

Read this reference only for the relevant task. The [skill core](../SKILL.md)
contains authorization rules and routes to other bundled topics; examples here
are not permission to run mutating commands. Check `boxyard <command> --help`
for the installed CLI. Versioned observations and counts are historical, not
a live inventory.

## Write ownership (`owner`, `claim`, `release`, `discard-local`)

A box can have a **write owner**: the single machine allowed to push its DATA/CONF. A box with
**no owner is unrestricted**, exactly as before this feature existed. **`boxyard new` claims
the box for the creating machine by default** — pass `--no-claim` when the box will be worked
on elsewhere; `boxyard include --read-only` includes a box without the nudge to claim it.
Ownership is recorded per box as `write_owner`
and compared against this machine's configured `machine_name` (configured, never derived
from the hostname, because hostnames are unreliable — one machine reports both
`lukas-pocket4` and `pocket4`).

```bash
boxyard owner --box-name NAME          # who may push this box (read-only; -o json too)
boxyard claim --box-name NAME          # make THIS machine the write owner
boxyard claim --all-included           # claim every box included here that has no owner
boxyard release --box-name NAME        # give up this machine's ownership
```

**If a sync is refused because another machine owns the box**, there are exactly two ways
out, and the error prints both:

```bash
boxyard claim --steal --box-name NAME  # take ownership from the current owner (prompts; -y to skip)
boxyard discard-local --box-name NAME  # throw away THIS machine's copy, take the remote's
```

`discard-local` is the destructive one, but not lossy: what it overwrites is kept under
the sync backups directory and the path is printed. Prefer `--steal` when this machine's
copy is the one you want to keep, `discard-local` when the remote's is.

**Ownership is also enforced on three commands that bypass sync entirely** and would
otherwise write to the remote unchecked: `force-push`, `rename --scope remote|both`, and
`delete`. Being refused by one of these is the gate working, not a bug — resolve it with
`claim`/`--steal` rather than reaching for a workaround.

## Health check (`doctor`)

`boxyard doctor` is a strictly read-only health check of the machine's whole boxyard state. It never mutates or auto-fixes anything, and exits 0 when healthy / 1 when there is any finding, so scripts and cron jobs can assert on it.

**Agents: run `boxyard doctor` whenever box state looks inconsistent** — e.g. a folder in `user_boxes_path` that `boxyard list` doesn't know about, `boxyard list` missing boxes that exist on another machine, group symlinks pointing nowhere, or errors mentioning boxmeta/sync records. Every finding comes with a one-line hint on how to fix it; apply the hints rather than improvising.

```bash
boxyard doctor                       # full check, including remote storage
boxyard doctor --no-remote           # offline: skip remote checks (stale-meta-mirror, tombstoned-box, diverged-box, write-denied, orphaned-snapshot, orphaned-remote-sync-backups)
boxyard doctor -o json               # machine-readable report
boxyard doctor -s STORAGE            # restrict the remote check to one storage location
```

Checks (33; `--no-remote` skips the six marked *remote*):

- **Registration and cache:** `unregistered-folder` (dirs — or stray files — in any checkout root not registered as boxes; the classic symptom of hand-creating folders instead of using `boxyard new`), `malformed-name` (names that don't parse as `<timestamp>_<subid>__<name>`; legacy formats are accepted), `broken-registration` (missing/invalid `boxmeta.toml` in the local store), `duplicate-box-id`, `stale-cache` (`boxyard_meta.json` disagrees with a fresh scan), `unknown-storage-location` (leftovers from removed/renamed storage locations), `tree-orphans` (parents referencing unknown box ids).
- **Group tree:** `dangling-symlinks` (group symlinks with missing targets), `group-tree-debris` (real files in the group tree, which break `create-user-symlinks` and thereby most mutating commands).
- **Sync state:** `orphaned-sync-records`, `interrupted-sync` (sync records left incomplete — the local copy may be incomplete; re-sync to recover), `diverged-box` (*remote*; both sides moved on independently, or a push never completed — sync refuses until resolved), `stale-meta-mirror` (*remote*; remote boxmetas not mirrored locally — what `sync-missing-meta` would fetch; a machine where that never runs silently hides newer boxes from `boxyard list`), `tombstoned-box` (*remote*; boxes deleted from another machine but still registered here), `orphaned-sync-backups` and `orphaned-remote-sync-backups` (the latter *remote*; backup dirs no sync record claims — may be a `discard-local` keepsake, never assume disposable).
- **Config and version skew:** `rclone-config` (missing rclone binary/remote sections/default exclude file), `unknown-config-keys` (typo, or config written for a newer boxyard), `unknown-boxmeta-keys` (box written by a newer boxyard; upgrade here), `machine-name-unset`.
- **Ownership:** `write-denied` (*remote*; another machine owns a box that has local changes here that can never be pushed), `stale-owner` (owner lacks a complete checkout, or looks renamed/retired), `unowned-box` (included here, unclaimed), `unpushed-meta-edit` (local `groups`/`parents`/`write_owner` edits not yet pushed).
- **Sync policies:** `sync-policy-conflict`, `unusable-box-sync-conf` (see "Sync policies").
- **Checkout roots:** `checkout-root-config` (overlapping/duplicate root paths), `checkout-root-unavailable`, `checkout-placement` (placement record missing/unloadable/contradicting what is on disk), `duplicate-checkout` (copies of one box in several roots), `interrupted-relocation` (recover with `boxyard relocate`).
- **Storage format:** `storage-format-mismatch` (box's actual format differs from what policy asks for; nothing converts automatically), `orphaned-snapshot` (*remote*; restic snapshots the pointer does not reach — usually a push that raced another machine; nothing is lost).

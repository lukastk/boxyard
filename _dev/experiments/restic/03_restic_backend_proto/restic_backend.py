"""
Prototype restic DATA backend for boxyard. THROWAWAY — see EXPERIMENTS_PLAN.md.

Scope: the DATA part only. META and CONF stay plain files, because
`sync-missing-meta`'s bulk depth-2 listing over `boxes/*/boxmeta.toml` is what
box discovery rests on and an opaque snapshot would destroy it.

The whole design is four moving parts:

1. **One repo per box**, at `boxes/<index_name>/data.restic/`. Measured
   justification: a single shared repo saves 7.0% of stored bytes across
   mymain's whole 266 GiB checkout, and costs per-box independence, keyless
   skip-filtering, and `delete` being a directory removal.

2. **A plain pointer file**, `boxes/<index_name>/data.snapshot`, holding the id
   of the snapshot the remote currently considers current. It sits at depth 2
   beside `boxmeta.toml`, so the bulk listing `--skip-unchanged-meta` ALREADY
   runs answers "did this box's DATA move" for the whole yard in the same single
   call. The repo's own `snapshots/` directory stays the truth; the pointer is a
   hint, and a stale hint costs one repo open, never correctness.

3. **A local state record**, mirroring `_sync_policy`'s check records: which
   snapshot this machine last pushed or restored. This is what replaces the
   local half of the SyncRecord/ULID pair.

4. **Pull is diff-driven.** A full `restic restore` costs O(destination tree)
   whatever changed (measured 14-65 s for a 134k-file box); `restic diff` plus
   `restore --include` costs O(change) (measured 3.1-3.3 s for the same box with
   50 files edited, and byte-identical to the full restore). `--include` does
   NOT delete, so removals are applied explicitly from the diff.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# The remote layout. `data.restic` deliberately does NOT collide with the plain
# `data/`, so a converted box and an unconverted one can coexist during the
# migration window and a half-finished conversion is never ambiguous.
BOX_RESTIC_REL_PATH = "data.restic"
BOX_SNAPSHOT_POINTER_REL_PATH = "data.snapshot"
RESTIC_STATE_REL_PATH = "restic_state"


class ResticCondition(Enum):
    """
    What a restic-backed DATA part needs. Deliberately named to line up with
    `SyncCondition` so the mapping into `sync_box` is mechanical.
    """

    SYNCED = "synced"
    NEEDS_PUSH = "needs_push"
    NEEDS_PULL = "needs_pull"
    CONFLICT = "conflict"
    # The remote has no repo yet: this box has never been pushed in this format.
    UNINITIALISED = "uninitialised"


@dataclass(frozen=True)
class ResticStatus:
    condition: ResticCondition
    remote_snapshot: str | None
    local_snapshot: str | None
    local_modified: bool


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


def _rclone_program(rclone_config_path: Path, rclone_binary: str = "rclone") -> str:
    """
    The `-o rclone.program` restic needs so that it reaches the remote through
    BOXYARD'S OWN rclone config rather than a second, divergent one.

    This is the property that makes restic acceptable at all: it inherits
    boxyard's storage-location abstraction instead of replacing it with restic's
    own much narrower set of backends.
    """
    return f"{rclone_binary} --config {rclone_config_path}"


def repo_url(store_path: Path, storage_location: str, remote_index_name: str) -> str:
    return (
        f"rclone:{storage_location}:"
        f"{(store_path / 'boxes' / remote_index_name / BOX_RESTIC_REL_PATH).as_posix()}"
    )


def restic_env(password: str, cache_dir: Path) -> dict[str, str]:
    """
    The password is passed in the CHILD ENVIRONMENT, fetched once per process.

    Not `--password-command`: restic would run it on every invocation, and
    `secret get` measured 0.77-1.51 s. At one invocation per box that is ~8
    minutes of 1Password round trips per 590-box pass, for a value that does not
    change.
    """
    env = dict(os.environ)
    env["RESTIC_PASSWORD"] = password
    env["RESTIC_CACHE_DIR"] = str(cache_dir)
    return env


def _restic(
    args: list[str],
    *,
    repo: str,
    rclone_program: str,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess:
    cmd = ["restic", "-o", f"rclone.program={rclone_program}", "-r", repo, *args]
    # argv, never a shell string: a box name reaches this from the filesystem and
    # may contain anything. See AGENTS.md.
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=check)


# ---------------------------------------------------------------------------
# The pointer file and the local state record
# ---------------------------------------------------------------------------


def read_remote_pointer(
    rclone_config_path: Path, storage_location: str, store_path: Path, remote_index_name: str
) -> str | None:
    """
    The remote's current snapshot id, from the plain pointer file.

    A single-box read here for clarity; the real caller gets this for the WHOLE
    yard out of the bulk depth-2 listing it already runs, at no extra cost.
    """
    path = store_path / "boxes" / remote_index_name / BOX_SNAPSHOT_POINTER_REL_PATH
    proc = subprocess.run(
        ["rclone", "--config", str(rclone_config_path), "cat", f"{storage_location}:{path.as_posix()}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    text = proc.stdout.strip()
    return text or None


def write_remote_pointer(
    rclone_config_path: Path,
    storage_location: str,
    store_path: Path,
    remote_index_name: str,
    snapshot_id: str,
    tmp_dir: Path,
) -> None:
    tmp = tmp_dir / "data.snapshot"
    tmp.write_text(snapshot_id + "\n")
    path = store_path / "boxes" / remote_index_name / BOX_SNAPSHOT_POINTER_REL_PATH
    subprocess.run(
        [
            "rclone", "--config", str(rclone_config_path),
            "copyto", str(tmp), f"{storage_location}:{path.as_posix()}",
        ],
        check=True,
        capture_output=True,
    )


def state_path(boxyard_data_path: Path, box_index_name: str) -> Path:
    return boxyard_data_path / RESTIC_STATE_REL_PATH / box_index_name / "data.json"


def read_state(boxyard_data_path: Path, box_index_name: str) -> dict | None:
    """
    Which snapshot this machine last agreed with the remote about.

    Degrades in ONE direction, exactly like `_sync_policy`'s check records:
    missing or unreadable means "I do not know", which every caller must read as
    "do the work". Losing it costs a full restore, never correctness.
    """
    p = state_path(boxyard_data_path, box_index_name)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) and rec.get("snapshot") else None


def write_state(boxyard_data_path: Path, box_index_name: str, snapshot_id: str) -> None:
    p = state_path(boxyard_data_path, box_index_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"snapshot": snapshot_id}))
    os.replace(tmp, p)  # crash leaves the old record or the new one, never half


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def local_is_modified(data_path: Path, snapshot_id: str | None, repo: str,
                      rclone_program: str, env: dict[str, str]) -> bool:
    """
    Does this machine hold DATA changes the remote has not seen?

    Answered with `backup --dry-run` against the recorded snapshot as parent:
    restic reports what it WOULD add, and zero added bytes means nothing to
    push. This reuses restic's own change detection rather than re-deriving one
    from mtimes, which is what `get_sync_status` has to do for plain trees.
    """
    args = ["backup", "--dry-run", "--no-scan", "--json", str(data_path)]
    if snapshot_id:
        args += ["--parent", snapshot_id]
    proc = _restic(args, repo=repo, rclone_program=rclone_program, env=env, check=False)
    if proc.returncode != 0:
        return True  # cannot tell -> assume changed; costs work, never correctness
    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("message_type") == "summary":
            # `dirs_changed` is deliberately NOT consulted. A no-op backup
            # reports "Dirs: 0 new, 1 changed" every single time -- the root
            # directory's own metadata is re-read -- so including it would make
            # every box permanently look modified and defeat the whole skip
            # filter. Measured on a 134k-file box: files_new=0, files_changed=0,
            # dirs_changed=1 for a run that added 348 bytes.
            return bool(
                msg.get("files_new") or msg.get("files_changed")
                or msg.get("dirs_new")
            )
    return True


def get_status(
    *,
    data_path: Path,
    repo: str,
    rclone_program: str,
    env: dict[str, str],
    remote_snapshot: str | None,
    local_snapshot: str | None,
) -> ResticStatus:
    """
    Map the (remote pointer, local record, local tree) triple onto a condition.

    This is the restic replacement for the SyncRecord/ULID comparison, and it is
    strictly better informed: snapshots are a linear history with ids and
    parents, so "the remote moved past me" and "we both moved" are
    DISTINGUISHABLE facts rather than a timestamp comparison.
    """
    if remote_snapshot is None:
        return ResticStatus(ResticCondition.UNINITIALISED, None, local_snapshot, True)

    modified = local_is_modified(data_path, local_snapshot, repo, rclone_program, env)

    if local_snapshot == remote_snapshot:
        return ResticStatus(
            ResticCondition.NEEDS_PUSH if modified else ResticCondition.SYNCED,
            remote_snapshot, local_snapshot, modified,
        )

    # The remote has moved since this machine last agreed with it.
    if modified:
        return ResticStatus(ResticCondition.CONFLICT, remote_snapshot, local_snapshot, True)
    return ResticStatus(ResticCondition.NEEDS_PULL, remote_snapshot, local_snapshot, False)


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def push(
    *,
    data_path: Path,
    repo: str,
    rclone_program: str,
    env: dict[str, str],
    parent: str | None,
    excludes: list[str],
    rclone_config_path: Path,
    storage_location: str,
    store_path: Path,
    remote_index_name: str,
    boxyard_data_path: Path,
    box_index_name: str,
    tmp_dir: Path,
) -> str:
    """Back the box up and publish the new snapshot id. Returns the id."""
    args = ["backup", "--no-scan", "--json"]
    for e in excludes:
        args += ["--exclude", e]
    if parent:
        args += ["--parent", parent]
    args.append(str(data_path))
    proc = _restic(args, repo=repo, rclone_program=rclone_program, env=env)

    snapshot_id = None
    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("message_type") == "summary":
            snapshot_id = msg.get("snapshot_id")
    if not snapshot_id:
        raise RuntimeError(f"restic backup reported no snapshot id:\n{proc.stdout}\n{proc.stderr}")

    # Pointer AFTER the snapshot exists. The other order would advertise a
    # snapshot that is not there yet, and a puller would fail on a repo that is
    # actually fine.
    write_remote_pointer(
        rclone_config_path, storage_location, store_path, remote_index_name,
        snapshot_id, tmp_dir,
    )
    write_state(boxyard_data_path, box_index_name, snapshot_id)
    return snapshot_id


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


def snapshot_source_path(
    snapshot_id: str, *, repo: str, rclone_program: str, env: dict[str, str]
) -> str | None:
    """
    The absolute path the pusher backed this snapshot up FROM.

    Needed on every pull, because restic records the pusher's absolute path in
    the snapshot and offers no way to normalise it (no `--set-path`; `rewrite`
    can change host and time but not paths; backing up through a symlink at a
    canonical path archives the SYMLINK, not the tree; `cd` plus a relative
    argument still resolves to absolute). All four were tested.

    That path is what lets `restore <snap>:<source>` place the CONTENTS at an
    arbitrary local directory, which is what makes a box restorable onto a
    machine whose checkout root differs -- mymain alone has two (`~/dev` and
    `~/hetzner_volume/boxes`), and the Macs have `/Users/...`.
    """
    proc = _restic(
        ["snapshots", snapshot_id, "--json"],
        repo=repo, rclone_program=rclone_program, env=env, check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        snaps = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return snaps[0]["paths"][0] if snaps and snaps[0].get("paths") else None


def _parse_diff(stdout: str, source_prefix: str) -> tuple[list[str], list[str]]:
    """
    (changed-or-added, removed) as paths RELATIVE to the snapshot's source root.

    restic prints one line per path prefixed `+`, `-`, `M`, `U` or `T`, using the
    absolute path recorded in the snapshot. Only `-` means "gone in the newer
    snapshot"; everything else needs restoring.
    """
    changed, removed = [], []
    for line in stdout.splitlines():
        if len(line) < 2 or line[0] not in "+-MUT" or line[1] != " ":
            continue
        mark, path = line[0], line[2:].strip()
        if not path.startswith(source_prefix):
            continue
        rel = path[len(source_prefix):].strip("/")
        if not rel:
            continue
        (removed if mark == "-" else changed).append(rel)
    return changed, removed


def _full_restore(target_snapshot, source, data_path, repo, rclone_program, env):
    """
    `restore <snap>:<source> --target <dir>` puts the snapshot's CONTENTS at
    `dir`, rather than at `dir/<absolute source path>` which the plain form
    produces. That is what makes the local checkout path independent of the
    pusher's.
    """
    _restic(
        ["restore", f"{target_snapshot}:{source}", "--target", str(data_path), "--delete"],
        repo=repo, rclone_program=rclone_program, env=env,
    )


def pull(
    *,
    data_path: Path,
    repo: str,
    rclone_program: str,
    env: dict[str, str],
    target_snapshot: str,
    base_snapshot: str | None,
    boxyard_data_path: Path,
    box_index_name: str,
) -> dict:
    """
    Bring the local tree to `target_snapshot`.

    Two paths, and the choice is the point of the design:

    * **Full** — `restore <snap>:<source> --target <data> --delete`. Correct
      always. O(destination tree): measured 24 s cold and 14-65 s warm for a
      134k-file box, because restic reads every existing file to compare.
    * **Targeted** — `restic diff` the two snapshots, then restore only what
      moved with `--include /<relative path>`, then apply removals. O(change):
      measured 3.1-3.3 s for the same box with 50 files edited, and verified
      byte-identical to the full restore.

    The full path is taken whenever the targeted one cannot be trusted:
      - no base recorded (never restored here, or the record was lost)
      - the two snapshots were pushed from DIFFERENT absolute source paths.
        `restic diff` compares by absolute path, so two snapshots of identical
        content under different roots come out as "everything added, everything
        removed" -- verified directly. Taking the full path there costs time;
        acting on that diff would delete the box and restore it.
      - `restic diff` failed at all (a base the repo no longer holds).
    Every fallback is in the safe direction: more work, never a wrong result.

    `restore --include` does NOT delete -- verified: a file absent from the newer
    snapshot survives an --include restore and is only removed by a full
    `--delete`. So removals are applied here explicitly, or a file deleted on
    another machine would silently come back, which is the exact class of bug
    tombstones exist to prevent.
    """
    data_path.mkdir(parents=True, exist_ok=True)
    target_source = snapshot_source_path(
        target_snapshot, repo=repo, rclone_program=rclone_program, env=env
    )
    if target_source is None:
        raise RuntimeError(f"snapshot {target_snapshot} has no recorded source path")

    def _full(mode):
        _full_restore(target_snapshot, target_source, data_path, repo, rclone_program, env)
        write_state(boxyard_data_path, box_index_name, target_snapshot)
        return {"mode": mode, "changed": None, "removed": None}

    if base_snapshot is None or base_snapshot == target_snapshot:
        return _full("full")

    base_source = snapshot_source_path(
        base_snapshot, repo=repo, rclone_program=rclone_program, env=env
    )
    if base_source != target_source:
        return _full("full-path-mismatch")

    diff = _restic(
        ["diff", base_snapshot, target_snapshot],
        repo=repo, rclone_program=rclone_program, env=env, check=False,
    )
    if diff.returncode != 0:
        return _full("full-fallback")

    changed, removed = _parse_diff(diff.stdout, target_source)

    if changed:
        args = ["restore", f"{target_snapshot}:{target_source}", "--target", str(data_path)]
        for rel in changed:
            # A LEADING SLASH anchors the pattern to the subpath root. Without
            # it restic matches the basename anywhere in the tree: `--include
            # package.json` restored 98 unrelated files in testing.
            args += [f"--include=/{rel}"]
        _restic(args, repo=repo, rclone_program=rclone_program, env=env)

    for rel in removed:
        victim = data_path / rel
        # Confined to the box's own DATA directory. A path out of the repo is
        # data, not an instruction, and `..` in it must not escape.
        try:
            victim.resolve().relative_to(data_path.resolve())
        except ValueError:
            continue
        if victim.is_dir() and not victim.is_symlink():
            shutil.rmtree(victim, ignore_errors=True)
        elif victim.exists() or victim.is_symlink():
            victim.unlink(missing_ok=True)

    write_state(boxyard_data_path, box_index_name, target_snapshot)
    return {"mode": "diff", "changed": len(changed), "removed": len(removed)}

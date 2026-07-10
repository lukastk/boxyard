"""Throwaway prototype of the B1 exec-bit sidecar manifest.

Records, per regular file, whether it is executable (owner-x bit). Stored as a
sorted JSON dict {relpath: bool} at the box-data root so it syncs as ordinary
content over ANY rclone backend (incl. SFTP, which can't carry mode metadata).

generate(root): (re)write the manifest from the current tree.
apply(root):    chmod files to match the manifest (idempotent).

Apply semantics: exec -> mirror the read bits into the exec bits (644->755,
664->775); non-exec -> clear all exec bits. No umask surprises, deterministic.
Symlinks and the manifest file itself are skipped.
"""
from __future__ import annotations
import json, os, stat
from pathlib import Path

MANIFEST_NAME = ".boxyard-perms.json"


def _iter_files(root: Path):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.is_symlink():          # boxyard ships symlinks via rclone --links
                continue
            if p.name == MANIFEST_NAME and p.parent == root:
                continue
            yield p


def generate(root: str | Path) -> dict[str, bool]:
    root = Path(root)
    entries: dict[str, bool] = {}
    for p in _iter_files(root):
        rel = p.relative_to(root).as_posix()
        entries[rel] = bool(p.stat().st_mode & stat.S_IXUSR)
    (root / MANIFEST_NAME).write_text(json.dumps(entries, indent=1, sort_keys=True) + "\n")
    return entries


def apply(root: str | Path) -> list[tuple[str, int, int]]:
    """Apply manifest to the tree. Returns [(relpath, old_mode, new_mode)] for changed files."""
    root = Path(root)
    mf = root / MANIFEST_NAME
    if not mf.is_file():
        raise FileNotFoundError(f"no manifest at {mf}")
    entries: dict[str, bool] = json.loads(mf.read_text())
    changed = []
    for rel, want_exec in entries.items():
        p = root / rel
        if p.is_symlink() or not p.is_file():
            continue
        cur = stat.S_IMODE(p.stat().st_mode)
        if want_exec:
            x_from_r = (cur & 0o444) >> 2          # mirror read bits into exec bits
            new = cur | x_from_r
        else:
            new = cur & ~0o111
        if new != cur:
            os.chmod(p, new)
            changed.append((rel, cur, new))
    return changed


if __name__ == "__main__":
    import sys
    cmd, root = sys.argv[1], sys.argv[2]
    if cmd == "generate":
        print(json.dumps(generate(root), indent=1, sort_keys=True))
    elif cmd == "apply":
        for rel, old, new in apply(root):
            print(f"{rel}: {old:o} -> {new:o}")
    else:
        sys.exit(f"unknown cmd {cmd}")

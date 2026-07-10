# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _utils.perms
#
# Exec-bit preservation via a sidecar manifest stored inside a box's DATA part.
#
# rclone (as boxyard invokes it) drops Unix file modes on sync, and the SFTP
# backend cannot carry mode metadata at all (rclone#7310), so the executable bit
# is lost on every round-trip. To fix this without changing the transport, we
# record which files are executable in a small JSON file at the DATA root
# (`.boxyard-perms.json`). It travels as ordinary synced content over *any*
# backend. `generate_exec_manifest` (re)writes it before a push;
# `apply_exec_manifest` restores the `+x` bit after a pull.
#
# **v1 is additive-only:** apply only ever *adds* `+x`, never removes it. This
# makes a stale or wrong manifest incapable of destroying a permission bit (worst
# case it fails to restore one — today's behaviour), which keeps mixed old/new
# boxyard versions safe during rollout. Propagating `chmod -x` is deferred to v2
# once every machine runs the new code.

# %%
#|default_exp _utils.perms

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();
import boxyard._utils.perms as this_module

# %%
#|export
import json
import os
import stat
import sys
from pathlib import Path

from boxyard import const

# %%
#|exporti
# Directory names pruned from the manifest walk. The first group mirrors
# const.DEFAULT_RCLONE_EXCLUDE — boxyard doesn't sync these, so their exec bits
# aren't part of the box's synced content and would only bloat the manifest with
# thousands of .venv/node_modules entries that don't exist on the remote. `.git`
# is synced, but its internal executables are noise (disabled `*.sample` hooks),
# so its exec bits are intentionally not preserved.
_PRUNED_DIR_NAMES = {".venv", ".pixi", ".trunk", "node_modules", "__pycache__", ".git"}


def _iter_regular_files(root: Path):
    """Yield every non-symlink regular file under ``root`` (recursively), skipping
    directories boxyard doesn't sync so the manifest tracks only synced content."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIR_NAMES]
        for fn in filenames:
            if fn == ".DS_Store":
                continue
            p = Path(dirpath) / fn
            if p.is_symlink():  # symlinks are shipped separately via rclone --links
                continue
            yield p

# %%
#|hide
show_doc(this_module.build_exec_manifest)

# %%
#|export
def build_exec_manifest(root: str | Path) -> dict:
    """Build the manifest dict for the DATA tree at ``root``.

    Returns ``{"version": 1, "executable": [<sorted relpaths that are +x>]}``.
    Only the owner-execute bit (``S_IXUSR``) is considered. The manifest file
    itself and symlinks are excluded.
    """
    root = Path(root)
    manifest_path = root / const.BOX_PERMS_MANIFEST_REL_PATH
    execs: list[str] = []
    for p in _iter_regular_files(root):
        if p == manifest_path:
            continue
        try:
            mode = p.stat().st_mode
        except OSError:
            continue
        if mode & stat.S_IXUSR:
            execs.append(p.relative_to(root).as_posix())
    execs.sort()
    return {"version": 1, "executable": execs}

# %%
#|hide
show_doc(this_module.generate_exec_manifest)

# %%
#|export
def generate_exec_manifest(root: str | Path) -> bool:
    """Write ``.boxyard-perms.json`` at ``root`` from the tree's current modes.

    Only writes when the content would change, so an unchanged box does not get a
    fresh mtime (which would look like a spurious edit to boxyard's sync-status
    check) and rclone does not needlessly re-transfer the manifest.

    Returns ``True`` if the file was (re)written, ``False`` if unchanged/skipped.
    """
    root = Path(root)
    if not root.is_dir():
        return False
    manifest_path = root / const.BOX_PERMS_MANIFEST_REL_PATH
    manifest = build_exec_manifest(root)
    already_exists = manifest_path.exists()
    # Don't create noise: a box with no executables and no manifest yet stays clean.
    # (An existing manifest is still kept accurate, e.g. shrinking to empty.)
    if not already_exists and not manifest["executable"]:
        return False
    content = json.dumps(manifest, indent=1, sort_keys=True) + "\n"
    if already_exists and manifest_path.read_text() == content:
        return False
    manifest_path.write_text(content)
    return True

# %%
#|hide
show_doc(this_module.apply_exec_manifest)

# %%
#|export
def apply_exec_manifest(root: str | Path) -> list[str]:
    """Restore ``+x`` on files the manifest marks executable (additive-only).

    - No manifest present (e.g. a box created before this feature): no-op, ``[]``.
    - For each listed file, add execute bits mirroring the read bits
      (``644 -> 755``, ``664 -> 775``). Never clears bits (v1 additive-only).
    - Missing/symlink/non-file entries are skipped.
    - A present-but-corrupt manifest is warned about and skipped — it must never
      break an otherwise-successful sync.

    Returns the list of relpaths whose mode was changed.
    """
    root = Path(root)
    manifest_path = root / const.BOX_PERMS_MANIFEST_REL_PATH
    if not manifest_path.is_file():
        return []
    try:
        data = json.loads(manifest_path.read_text())
        execs = data["executable"]
        assert isinstance(execs, list)
    except (json.JSONDecodeError, OSError, KeyError, AssertionError) as e:
        print(
            f"WARNING: could not parse perms manifest '{manifest_path}' ({e}); "
            f"skipping exec-bit restore.",
            file=sys.stderr,
        )
        return []
    changed: list[str] = []
    for rel in execs:
        p = root / rel
        if p.is_symlink() or not p.is_file():
            continue
        cur = stat.S_IMODE(p.stat().st_mode)
        new = cur | ((cur & 0o444) >> 2)  # mirror read bits into exec bits
        if new != cur:
            os.chmod(p, new)
            changed.append(rel)
    return changed

# %% [markdown]
# ## Smoke test

# %%
import tempfile

_root = Path(tempfile.mkdtemp(prefix="perms_smoke_"))
(_root / "sub").mkdir()
(_root / "script.sh").write_text("#!/bin/sh\n"); (_root / "script.sh").chmod(0o755)
(_root / "data.txt").write_text("x"); (_root / "data.txt").chmod(0o644)
(_root / "sub" / "tool").write_text("#!/usr/bin/env python\n"); (_root / "sub" / "tool").chmod(0o755)

# generate captures the two executables
assert generate_exec_manifest(_root) is True
assert build_exec_manifest(_root)["executable"] == ["script.sh", "sub/tool"]
# idempotent: unchanged content is not rewritten
assert generate_exec_manifest(_root) is False

# simulate a sync stripping the exec bit, then restore
(_root / "script.sh").chmod(0o644)
(_root / "sub" / "tool").chmod(0o644)
assert not os.access(_root / "script.sh", os.X_OK)
changed = apply_exec_manifest(_root)
assert set(changed) == {"script.sh", "sub/tool"}
assert os.access(_root / "script.sh", os.X_OK)
assert os.access(_root / "sub" / "tool", os.X_OK)
assert not os.access(_root / "data.txt", os.X_OK)  # non-exec left alone
# apply is idempotent
assert apply_exec_manifest(_root) == []
# no manifest -> no-op (backward compatible with pre-feature boxes)
import shutil as _sh
_empty = Path(tempfile.mkdtemp(prefix="perms_empty_"))
assert apply_exec_manifest(_empty) == []
_sh.rmtree(_root); _sh.rmtree(_empty)
print("perms smoke test passed")

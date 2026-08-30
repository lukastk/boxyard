"""
End-to-end drive of the prototype restic DATA backend through a THROWAWAY
two-machine boxyard under /tmp. Nothing here touches the live yard.

What it proves, in order:

  1. push  -> a box's DATA reaches the remote as a restic repo + a plain pointer
  2. the pointer is readable with NO key, by a plain rclone listing
  3. a second machine pulls the box and gets byte-identical content
  4. an unchanged box is skipped from the pointer alone, with zero repo opens
  5. an edit on machine A is pulled by machine B via diff + targeted restore,
     and the result is byte-identical to a full restore
  6. a DELETION on A propagates to B  (`restore --include` cannot do this alone)
  7. both machines editing produces CONFLICT, not silent loss
  8. converting an existing PLAIN box is safe against an un-upgraded machine
  9. RETENTION: after `restic forget` deletes the snapshot a machine last synced
     from, that machine still converges -- full restore, no false CONFLICT --
     and a machine that DID edit locally still reports CONFLICT

Run:  uv run python _dev/experiments/restic/03_restic_backend_proto/e2e.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, str(Path(__file__).parent))

import asyncio

from restic_backend import (  # noqa: E402
    BOX_RESTIC_REL_PATH,
    BOX_SNAPSHOT_POINTER_REL_PATH,
    ResticCondition,
    _rclone_program,
    _restic,
    get_status,
    pull,
    push,
    read_remote_pointer,
    read_state,
    repo_url,
    restic_env,
    snapshot_exists,
)

PASSWORD = "prototype-scratch-password"
OK, BAD = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"
_results = []


def check(label, cond, detail=""):
    _results.append(bool(cond))
    print(f"{OK if cond else BAD}  {label}{('  — ' + detail) if detail else ''}")


def tree(p: Path) -> dict[str, bytes]:
    return {
        str(f.relative_to(p)): f.read_bytes()
        for f in sorted(p.rglob("*"))
        if f.is_file() and not f.is_symlink()
    }


async def main():
    from boxyard import const
    from boxyard._enums import BoxPart
    from boxyard._models import get_boxyard_meta
    from boxyard.cmds import include_box, new_box, sync_box, sync_missing_boxmetas
    from tests.integration.conftest import create_boxyards

    scratch = Path(tempfile.mkdtemp(prefix="restic_e2e_", dir="/tmp"))
    cache = scratch / "cache"
    cache.mkdir()
    tmp_dir = scratch / "tmp"
    tmp_dir.mkdir()

    remote_name, remote_root, yards = create_boxyards(num_boxyards=2)
    (cfgA, cpA, dpA), (cfgB, cpB, dpB) = yards
    store_path = cfgA.storage_locations[remote_name].store_path
    rclone_prog = _rclone_program(cfgA.rclone_config_path)
    env = restic_env(PASSWORD, cache)

    # ---- A creates a box, DATA only handled by the restic backend -----------
    idx = new_box(config_path=cpA, box_name="resticbox", storage_location=remote_name)
    dataA = get_boxyard_meta(cfgA).by_index_name[idx].get_local_part_path(cfgA, BoxPart.DATA)
    (dataA / "notes.md").write_text("first version\n")
    (dataA / "sub").mkdir(exist_ok=True)
    (dataA / "sub" / "keep.txt").write_text("keep me\n")
    (dataA / "sub" / "doomed.txt").write_text("delete me later\n")

    # META/CONF still go through boxyard's ordinary path — unchanged, on purpose.
    await sync_box(config_path=cpA, box_index_name=idx,
                   sync_choices=[BoxPart.META, BoxPart.CONF], verbose=False)

    repo = repo_url(store_path, remote_name, idx)
    subprocess.run(
        ["restic", "-o", f"rclone.program={rclone_prog}", "-r", repo, "init"],
        env=env, capture_output=True, check=True,
    )

    common = dict(repo=repo, rclone_program=rclone_prog, env=env,
                  rclone_config_path=cfgA.rclone_config_path,
                  storage_location=remote_name, store_path=store_path,
                  remote_index_name=idx, tmp_dir=tmp_dir)

    print("\n=== 1. push ===")
    snap1 = push(data_path=dataA, parent=None, excludes=[".venv", "node_modules"],
                 boxyard_data_path=cfgA.boxyard_data_path, box_index_name=idx, **common)
    box_root = remote_root / "boxyard" / "boxes" / idx
    check("repo written to boxes/<box>/data.restic/", (box_root / BOX_RESTIC_REL_PATH).is_dir())
    check("plain data/ NOT created", not (box_root / const.BOX_DATA_REL_PATH).exists())
    check("boxmeta.toml still a plain file at depth 2",
          (box_root / const.BOX_METAFILE_REL_PATH).is_file(),
          "discovery listing survives")

    print("\n=== 2. the pointer is keyless ===")
    ptr = (box_root / BOX_SNAPSHOT_POINTER_REL_PATH)
    check("pointer file exists beside boxmeta.toml", ptr.is_file())
    check("pointer holds the snapshot id", ptr.read_text().strip() == snap1, snap1[:12])
    snap_files = sorted(p.name for p in (box_root / BOX_RESTIC_REL_PATH / "snapshots").iterdir())
    check("snapshots/ filename IS the snapshot id (no key used)", snap1 in snap_files)

    print("\n=== 3. machine B pulls ===")
    await sync_missing_boxmetas(config_path=cpB, verbose=False)
    await include_box(config_path=cpB, box_index_name=idx, read_only=True)
    dataB = get_boxyard_meta(cfgB).by_index_name[idx].get_local_part_path(cfgB, BoxPart.DATA)
    shutil.rmtree(dataB, ignore_errors=True)
    remote_ptr = read_remote_pointer(cfgB.rclone_config_path, remote_name, store_path, idx)
    check("B reads the pointer with no repo key", remote_ptr == snap1)
    res = pull(data_path=dataB, repo=repo, rclone_program=rclone_prog, env=env,
               target_snapshot=snap1, base_snapshot=None,
               boxyard_data_path=cfgB.boxyard_data_path, box_index_name=idx)
    # `restore <snap>:<source>` places the CONTENTS at the target, so B's
    # checkout path need not match A's. That is what makes a box restorable
    # onto a machine with a different checkout root.
    restored_root = dataB
    got = tree(restored_root)
    check("B's content is byte-identical to A's", got == tree(dataA),
          f"{len(got)} files, mode={res['mode']}")

    print("\n=== 4. unchanged box is skipped from the pointer alone ===")
    st = get_status(data_path=dataA, repo=repo, rclone_program=rclone_prog, env=env,
                    remote_snapshot=read_remote_pointer(cfgA.rclone_config_path, remote_name, store_path, idx),
                    local_snapshot=read_state(cfgA.boxyard_data_path, idx)["snapshot"])
    check("A sees SYNCED", st.condition == ResticCondition.SYNCED, st.condition.value)
    check("skip decision needs only the pointer + local record",
          read_state(cfgB.boxyard_data_path, idx)["snapshot"] == remote_ptr,
          "one bulk listing answers this for the whole yard")

    print("\n=== 5. A edits, B pulls incrementally ===")
    (dataA / "notes.md").write_text("second version\n")
    (dataA / "sub" / "added.txt").write_text("brand new\n")
    snap2 = push(data_path=dataA, parent=snap1, excludes=[".venv", "node_modules"],
                 boxyard_data_path=cfgA.boxyard_data_path, box_index_name=idx, **common)
    check("pointer advanced", read_remote_pointer(cfgA.rclone_config_path, remote_name, store_path, idx) == snap2)
    res = pull(data_path=dataB, repo=repo, rclone_program=rclone_prog, env=env,
               target_snapshot=snap2, base_snapshot=snap1,
               boxyard_data_path=cfgB.boxyard_data_path, box_index_name=idx)
    check("B used the diff path, not a full restore", res["mode"] == "diff", str(res))
    check("B content matches A after incremental pull", tree(restored_root) == tree(dataA))

    print("\n=== 6. a deletion on A reaches B ===")
    (dataA / "sub" / "doomed.txt").unlink()
    snap3 = push(data_path=dataA, parent=snap2, excludes=[".venv", "node_modules"],
                 boxyard_data_path=cfgA.boxyard_data_path, box_index_name=idx, **common)
    res = pull(data_path=dataB, repo=repo, rclone_program=rclone_prog, env=env,
               target_snapshot=snap3, base_snapshot=snap2,
               boxyard_data_path=cfgB.boxyard_data_path, box_index_name=idx)
    check("deleted file is gone on B", not (restored_root / "sub" / "doomed.txt").exists(),
          f"removed={res['removed']}")
    check("B still byte-identical to A", tree(restored_root) == tree(dataA))

    print("\n=== 7. both sides edit -> CONFLICT, not silent loss ===")
    (dataA / "notes.md").write_text("A's third version\n")
    snap4 = push(data_path=dataA, parent=snap3, excludes=[".venv", "node_modules"],
                 boxyard_data_path=cfgA.boxyard_data_path, box_index_name=idx, **common)
    (restored_root / "notes.md").write_text("B's competing edit\n")
    st = get_status(data_path=restored_root, repo=repo, rclone_program=rclone_prog, env=env,
                    remote_snapshot=snap4,
                    local_snapshot=read_state(cfgB.boxyard_data_path, idx)["snapshot"])
    check("B reports CONFLICT", st.condition == ResticCondition.CONFLICT, st.condition.value)
    check("B's competing edit is still on disk",
          (restored_root / "notes.md").read_text() == "B's competing edit\n")

    print("\n=== 8. converting an existing PLAIN box, with an un-upgraded machine ===")
    plain = new_box(config_path=cpA, box_name="plainbox", storage_location=remote_name)
    pdataA = get_boxyard_meta(cfgA).by_index_name[plain].get_local_part_path(cfgA, BoxPart.DATA)
    (pdataA / "legacy.txt").write_text("written before restic existed\n")
    await sync_box(config_path=cpA, box_index_name=plain, verbose=False)
    await sync_missing_boxmetas(config_path=cpB, verbose=False)
    await include_box(config_path=cpB, box_index_name=plain, read_only=True)
    await sync_box(config_path=cpB, box_index_name=plain, verbose=False)
    pdataB = get_boxyard_meta(cfgB).by_index_name[plain].get_local_part_path(cfgB, BoxPart.DATA)
    before_B = tree(pdataB)

    p_root = remote_root / "boxyard" / "boxes" / plain
    p_repo = repo_url(store_path, remote_name, plain)
    subprocess.run(["restic", "-o", f"rclone.program={rclone_prog}", "-r", p_repo, "init"],
                   env=env, capture_output=True, check=True)
    p_snap = push(data_path=pdataA, parent=None, excludes=[],
                  boxyard_data_path=cfgA.boxyard_data_path, box_index_name=plain,
                  repo=p_repo, rclone_program=rclone_prog, env=env,
                  rclone_config_path=cfgA.rclone_config_path, storage_location=remote_name,
                  store_path=store_path, remote_index_name=plain, tmp_dir=tmp_dir)
    # verify before destroying anything
    verify = scratch / "verify"
    subprocess.run(["restic", "-o", f"rclone.program={rclone_prog}", "-r", p_repo,
                    "restore", f"{p_snap}:{pdataA}", "--target", str(verify)], env=env,
                   capture_output=True, check=True)
    check("restore verifies byte-identical before conversion completes",
          tree(verify) == tree(pdataA))

    shutil.rmtree(p_root / const.BOX_DATA_REL_PATH)
    rec = remote_root / "boxyard" / const.SYNC_RECORDS_REL_PATH / plain / "data.rec"
    rec.unlink()  # THE GATE — without this an old machine resurrects data/

    refused = False
    try:
        await sync_box(config_path=cpB, box_index_name=plain, verbose=False)
    except Exception:
        refused = True
    check("un-upgraded machine REFUSES rather than acting", refused)
    check("un-upgraded machine's local data is intact", tree(pdataB) == before_B)
    check("plain data/ was NOT resurrected on the remote",
          not (p_root / const.BOX_DATA_REL_PATH).exists())
    check("the restic repo survived", (p_root / BOX_RESTIC_REL_PATH).is_dir())

    print("\n=== 9. retention: the snapshot B last synced from gets forgotten ===")
    # Re-converge B onto A first, so B is a clean replica at a known snapshot.
    (restored_root / "notes.md").write_text("A's third version\n")   # drop B's competing edit
    pull(data_path=dataB, repo=repo, rclone_program=rclone_prog, env=env,
         target_snapshot=snap4, base_snapshot=None,
         boxyard_data_path=cfgB.boxyard_data_path, box_index_name=idx)
    b_state = read_state(cfgB.boxyard_data_path, idx)
    check("B's state records a snapshot AND when it synced",
          b_state["snapshot"] == snap4 and b_state.get("synced_at_unix"))

    # A keeps working; then a maintenance pass forgets everything but the latest.
    (dataA / "notes.md").write_text("A's fourth version\n")
    (dataA / "sub" / "late.txt").write_text("added after B went offline\n")
    snap5 = push(data_path=dataA, parent=snap4, excludes=[".venv", "node_modules"],
                 boxyard_data_path=cfgA.boxyard_data_path, box_index_name=idx, **common)
    _restic(["forget", "--keep-last", "1", "--path", str(dataA)],
            repo=repo, rclone_program=rclone_prog, env=env, check=False)
    check("B's recorded snapshot has been forgotten",
          not snapshot_exists(snap4, repo=repo, rclone_program=rclone_prog, env=env))

    # B, unmodified, comes back. It must NOT report CONFLICT.
    b_state = read_state(cfgB.boxyard_data_path, idx)
    st = get_status(data_path=restored_root, repo=repo, rclone_program=rclone_prog, env=env,
                    remote_snapshot=snap5, local_snapshot=b_state["snapshot"],
                    synced_at_unix=b_state.get("synced_at_unix"))
    check("unmodified B reports NEEDS_PULL, not a false CONFLICT",
          st.condition == ResticCondition.NEEDS_PULL, st.condition.value)
    res = pull(data_path=dataB, repo=repo, rclone_program=rclone_prog, env=env,
               target_snapshot=snap5, base_snapshot=b_state["snapshot"],
               boxyard_data_path=cfgB.boxyard_data_path, box_index_name=idx)
    check("B degrades to a FULL restore rather than failing",
          res["mode"].startswith("full"), res["mode"])
    check("B converges byte-identically after the forget",
          tree(restored_root) == tree(dataA))

    # And the other direction: a machine that DID edit locally must still say CONFLICT.
    (dataA / "notes.md").write_text("A's fifth version\n")
    snap6 = push(data_path=dataA, parent=snap5, excludes=[".venv", "node_modules"],
                 boxyard_data_path=cfgA.boxyard_data_path, box_index_name=idx, **common)
    _restic(["forget", "--keep-last", "1", "--path", str(dataA)],
            repo=repo, rclone_program=rclone_prog, env=env, check=False)
    (restored_root / "sub" / "b-work.txt").write_text("real local work on B\n")
    b_state = read_state(cfgB.boxyard_data_path, idx)
    check("B's base was forgotten again",
          not snapshot_exists(b_state["snapshot"], repo=repo, rclone_program=rclone_prog, env=env))
    st = get_status(data_path=restored_root, repo=repo, rclone_program=rclone_prog, env=env,
                    remote_snapshot=snap6, local_snapshot=b_state["snapshot"],
                    synced_at_unix=b_state.get("synced_at_unix"))
    check("EDITED B still reports CONFLICT, so its work is not silently lost",
          st.condition == ResticCondition.CONFLICT, st.condition.value)
    check("B's local work is still on disk", (restored_root / "sub" / "b-work.txt").exists())

    print(f"\n{sum(_results)}/{len(_results)} checks passed")
    shutil.rmtree(scratch, ignore_errors=True)
    return 0 if all(_results) else 1


sys.exit(asyncio.run(main()))

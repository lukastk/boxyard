"""ORACLE -- read-only validation of the full-pass skip filter against the live yard.

Writes NOTHING to the remote, and nothing to this machine's boxyard state: the
sidecars it needs are SYNTHESIZED in a scratch copy of the local
`sync_records/` tree, never written into `~/.boxyard`.

For every box in a slice of the registry it runs the REAL path's question --
`get_sync_status` for META, CONF and included plain DATA -- and, from the
remote record each returns (its md5, exactly as the real path would remember
it), synthesizes the sidecar that a SYNCED/EXCLUDED verdict WILL write. It then
asks `boxes_needing_sync_full` for its verdict, fed by the two bulk listings
`multi-sync` takes (the hashed `sync_records/` one included), and checks it
against the real answers:

    skippable  ==>  every probed part's condition is SYNCED or EXCLUDED   (else WRONG SKIP)

It also reports the "missed skip" set (needed, though the real path would have
been a no-op -- an inefficiency, never a danger), boxes the real path raised
on, and the conf-dir signal cross-checked against `remote_path_exists`.

Usage (from the boxyard repo, with the feature exported to src/):
    .venv/bin/python _dev/oracle_skip.py --offset 0 --limit 150 --out /tmp/oracle.jsonl
Run the slices back to back; `--summary /tmp/oracle.jsonl` prints the totals.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from boxyard import const
from boxyard._enums import BoxPart, StorageFormat
from boxyard._models import SyncCondition, get_boxyard_meta, get_sync_status
from boxyard._remote_identity import read_remote_identity, remote_identity_path, write_remote_identity
from boxyard._sync_policy import boxes_needing_sync_full, project_box_listing, project_record_listing, remote_view_for
from boxyard._tombstones import list_tombstoned_box_ids
from boxyard._utils import rclone_lsjson
from boxyard.config import StorageType, get_config

ACCEPTABLE = {SyncCondition.SYNCED, SyncCondition.EXCLUDED}


async def bulk_listings(config, box_metas):
    """The two listings multi-sync takes, with its exact filters."""
    boxes, records = {}, {}
    for sl_name in sorted({bm.storage_location for bm in box_metas}):
        sl = config.storage_locations[sl_name]
        if sl.storage_type == StorageType.LOCAL:
            continue
        t0 = time.time()
        boxes.update(project_box_listing(sl_name, await rclone_lsjson(
            config.rclone_config_path, source=sl_name,
            source_path=sl.store_path / const.REMOTE_BOXES_REL_PATH,
            recursive=True, max_depth=2,
            filter=[
                f"+ /*/{const.BOX_METAFILE_REL_PATH}",
                f"+ /*/{const.BOX_SNAPSHOT_POINTER_REL_PATH}",
                f"+ /*/{const.BOX_CONF_REL_PATH}/",
                f"+ /*/{const.BOX_CONF_REL_PATH}",
                f"+ /*/{const.BOX_DATA_REL_PATH}/",
                f"+ /*/{const.BOX_DATA_REL_PATH}",
                f"+ /*/{const.BOX_RESTIC_REL_PATH}/",
                "- **",
            ],
        )))
        t1 = time.time()
        records.update(project_record_listing(sl_name, await rclone_lsjson(
            config.rclone_config_path, source=sl_name,
            source_path=sl.store_path / const.SYNC_RECORDS_REL_PATH,
            files_only=True, recursive=True, max_depth=2,
            filter=["+ /*/*.rec", "- **"], md5=True,
        )))
        print(f"  {sl_name}: boxes/ {t1 - t0:.1f}s, hashed sync_records/ {time.time() - t1:.1f}s", file=sys.stderr)
    return boxes, records


async def real_status(config, bm, part):
    return await get_sync_status(
        rclone_config_path=config.rclone_config_path,
        local_path=bm.get_local_part_path(config, part),
        local_sync_record_path=bm.get_local_sync_record_path(config, part),
        remote=bm.storage_location,
        remote_path=bm.get_remote_part_path(config, part),
        remote_sync_record_path=bm.get_remote_sync_record_path(config, part),
        exclude_path=bm.get_effective_exclude_path(config) if part is BoxPart.DATA else None,
        local_absence_means_excluded=(part is BoxPart.DATA),
    )


class ScratchSidecars:
    """
    A copy of `~/.boxyard/sync_records` in which sidecars can be synthesized.
    The verdict reads sidecars beside the local record path, so the config's
    `boxyard_data_path` is pointed at the copy for the verdict call only.
    """

    def __init__(self, config):
        self.root = Path(tempfile.mkdtemp(prefix="oracle-skip-"))
        src = config.boxyard_data_path / const.SYNC_RECORDS_REL_PATH
        shutil.copytree(src, self.root / const.SYNC_RECORDS_REL_PATH, symlinks=True)
        # Everything else the verdict reads (placements, check records) stays
        # where it is: symlink the other entries of the data dir.
        for entry in config.boxyard_data_path.iterdir():
            if entry.name != const.SYNC_RECORDS_REL_PATH:
                (self.root / entry.name).symlink_to(entry)
        self.config = config.model_copy(update={"boxyard_data_path": self.root})

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


async def judge_box(config, scratch, bm, boxes, records, tombstoned, sem):
    from boxyard._checkout import LocalCheckoutState, get_box_checkout_status

    row = {"box": bm.index_name, "format": bm.storage_format.value, "parts": {}, "errors": {}}
    checkout = get_box_checkout_status(config, bm).state
    row["checkout"] = checkout.value
    statuses = {}
    for part in BoxPart:
        if part is BoxPart.DATA and (
            checkout not in (LocalCheckoutState.INCLUDED, LocalCheckoutState.EXCLUDED)
            or bm.storage_format is StorageFormat.RESTIC
        ):
            continue  # the real path raises / restic has its own filter
        async with sem:
            try:
                statuses[part] = await real_status(config, bm, part)
            except Exception as e:  # noqa: BLE001 -- recorded, never hidden
                row["errors"][part.value] = f"{type(e).__name__}: {e}"
    for part, st in statuses.items():
        row["parts"][part.value] = {
            "condition": st.sync_condition.value,
            "local_rec": str(st.local_sync_record.ulid) if st.local_sync_record else None,
            "remote_rec": str(st.remote_sync_record.ulid) if st.remote_sync_record else None,
            "remote_complete": bool(st.remote_sync_record and st.remote_sync_record.sync_complete),
            "remote_md5": st.remote_sync_record_md5,
            "remote_path_exists": st.remote_path_exists,
        }
        # What the real path WILL remember on this verdict -- into the scratch copy.
        if st.sync_condition in ACCEPTABLE and st.remote_sync_record is not None and st.remote_sync_record_md5:
            write_remote_identity(
                bm.get_local_sync_record_path(scratch.config, part),
                md5=st.remote_sync_record_md5,
                ulid=str(st.remote_sync_record.ulid),
                sync_complete=st.remote_sync_record.sync_complete,
            )

    live_verdict = boxes_needing_sync_full(
        config, [bm], requested_parts=list(BoxPart), records=records, boxes=boxes,
        tombstoned=tombstoned, skip_meta=True, skip_data=True,
    )
    row["skippable_today"] = bm.index_name in live_verdict.skippable

    verdict = boxes_needing_sync_full(
        scratch.config, [bm], requested_parts=list(BoxPart), records=records, boxes=boxes,
        tombstoned=tombstoned, skip_meta=True, skip_data=True,
    )
    row["skippable"] = bm.index_name in verdict.skippable
    row["reason"] = verdict.reasons.get(bm.index_name)

    if row["skippable"]:
        bad = {p.value: st.sync_condition.value for p, st in statuses.items() if st.sync_condition not in ACCEPTABLE}
        row["wrong_skip"] = bad or None
    else:
        row["wrong_skip"] = None
        row["missed_skip"] = bool(statuses) and all(st.sync_condition in ACCEPTABLE for st in statuses.values()) and not row["errors"]

    conf = statuses.get(BoxPart.CONF)
    view = remote_view_for(boxes, bm.storage_location, bm.box_id)
    if conf is not None and view is not None:
        row["conf_dir_listing"] = view.conf_dir
        row["conf_dir_real"] = conf.remote_path_exists
    return row


async def main(argv):
    offset = int(argv[argv.index("--offset") + 1]) if "--offset" in argv else 0
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None
    concurrency = int(argv[argv.index("--concurrency") + 1]) if "--concurrency" in argv else 3

    cache = Path(argv[argv.index("--listings-cache") + 1]) if "--listings-cache" in argv else None

    config = get_config(Path.home() / ".config" / "boxyard" / "config.toml")
    box_metas = sorted(get_boxyard_meta(config).box_metas, key=lambda b: b.index_name)
    chunk = box_metas[offset: offset + limit if limit else None]
    t0 = time.time()
    if cache is not None and cache.exists():
        import pickle

        boxes, records = pickle.loads(cache.read_bytes())
        print(f"listings: reused {cache} (a point-in-time view; fine for slices run back to back)", file=sys.stderr)
    else:
        boxes, records = await bulk_listings(config, box_metas)
        if cache is not None:
            import pickle

            cache.write_bytes(pickle.dumps((boxes, records)))
    tombstoned = set()
    for sl_name in sorted({bm.storage_location for bm in box_metas}):
        if config.storage_locations[sl_name].storage_type != StorageType.LOCAL:
            ids = await list_tombstoned_box_ids(config, sl_name)
            tombstoned |= {bm.index_name for bm in box_metas if bm.storage_location == sl_name and bm.box_id in ids}
    print(f"listings: {time.time() - t0:.1f}s; {len(records)} boxes with records, "
          f"{sum(1 for v in boxes.values() if v.conf_dir)} with a remote conf dir, "
          f"{sum(1 for v in boxes.values() if v.data_dir)} with data/, "
          f"{sum(1 for v in boxes.values() if v.restic_dir)} with data.restic/, "
          f"{sum(1 for v in boxes.values() if v.pointer)} pointers, "
          f"{sum(1 for v in boxes.values() if v.anomalies)} anomalies, {len(tombstoned)} tombstoned",
          file=sys.stderr)

    scratch = ScratchSidecars(config)
    try:
        sem = asyncio.Semaphore(concurrency)
        rows = await asyncio.gather(*(judge_box(config, scratch, bm, boxes, records, tombstoned, sem) for bm in chunk))
    finally:
        scratch.cleanup()
    if out:
        with out.open("a") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    wrong = [r for r in rows if r["wrong_skip"]]
    print(f"{len(chunk)} boxes in {time.time() - t0:.0f}s: {sum(r['skippable'] for r in rows)} skippable after one pass, "
          f"{sum(r['skippable_today'] for r in rows)} skippable today, {len(wrong)} WRONG SKIPS", file=sys.stderr)
    for r in wrong:
        print(f"WRONG SKIP: {r['box']} {r['wrong_skip']}", file=sys.stderr)
    return 1 if wrong else 0


def summary(path: Path):
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    print(f"{len(rows)} boxes judged")
    print(f"  skippable after one real-path pass: {sum(r['skippable'] for r in rows)}")
    print(f"  skippable today (no sidecars yet): {sum(r['skippable_today'] for r in rows)}")
    print(f"  WRONG skips: {sum(1 for r in rows if r['wrong_skip'])}")
    for r in rows:
        if r["wrong_skip"]:
            print(f"    {r['box']}: {r['wrong_skip']}")
    needed = [r for r in rows if not r["skippable"]]
    print(f"  needed: {len(needed)}; first failed gate: {dict(Counter(r['reason'] for r in needed))}")
    missed = [r for r in needed if r.get("missed_skip")]
    print(f"  missed skips (needed, yet every probed part SYNCED/EXCLUDED): {len(missed)}")
    print(f"    by reason: {dict(Counter(r['reason'] for r in missed))}")
    for r in missed[:10]:
        print(f"    {r['box']} [{r['checkout']}]: {r['reason']} {r['parts']}")
    errs = [r for r in rows if r["errors"]]
    print(f"  boxes whose real path raised: {len(errs)}")
    for r in errs[:10]:
        print(f"    {r['box']}: {r['errors']}")
    conf_mismatch = [r for r in rows if "conf_dir_listing" in r and r["conf_dir_listing"] != r["conf_dir_real"]]
    print(f"  conf-dir signal disagreements (listing vs real path): {len(conf_mismatch)}")
    for r in conf_mismatch[:10]:
        print(f"    {r['box']}: listing={r['conf_dir_listing']} real={r['conf_dir_real']}")
    conds = Counter()
    for r in rows:
        for p, d in r["parts"].items():
            conds[(p, d["condition"])] += 1
    print(f"  real-path conditions: {dict(conds)}")


if __name__ == "__main__":
    if "--summary" in sys.argv:
        summary(Path(sys.argv[sys.argv.index("--summary") + 1]))
        sys.exit(0)
    sys.exit(asyncio.run(main(sys.argv)))

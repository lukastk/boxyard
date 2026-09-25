"""ORACLE -- read-only validation of the full-pass skip filter against the live yard.

Writes NOTHING (no records, no markers, no baselines, no stamps).

For every box in a slice of the registry it runs the REAL path's question --
`get_sync_status` for META, CONF and DATA -- and, from the remote records that
returns, synthesizes the `RemoteRecordView` the bulk listing WILL show once
markers are bootstrapped. It then asks `boxes_needing_sync_full` for its verdict
and checks it against the real answers:

    skippable  ==>  every part's condition is SYNCED or EXCLUDED   (else WRONG SKIP)

It also reports, for boxes the filter calls needed, which part failed and why
the real path would have been a no-op anyway (the "missed skip" set -- an
inefficiency, never a danger), and cross-checks the listing's conf-dir signal
against `remote_path_exists` for CONF.

Usage (from the boxyard repo, with the feature exported to src/):
    .venv/bin/python _dev/oracle_skip.py --offset 0 --limit 150 --out /tmp/oracle.jsonl
Run the slices back to back; `--summary /tmp/oracle.jsonl` prints the totals.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

from boxyard import const
from boxyard._enums import BoxPart, StorageFormat
from boxyard._models import SyncCondition, get_boxyard_meta, get_sync_status
from boxyard._sync_policy import RemoteRecordView, boxes_needing_sync_full, project_record_listing
from boxyard._tombstones import list_tombstoned_box_ids
from boxyard._utils import rclone_lsjson
from boxyard.config import StorageType, get_config

ACCEPTABLE = {SyncCondition.SYNCED, SyncCondition.EXCLUDED}


async def bulk_listings(config, box_metas):
    """The two listings multi-sync takes, with its exact filters."""
    pointer, conf_dirs, record_entries = {}, set(), []
    for sl_name in sorted({bm.storage_location for bm in box_metas}):
        sl = config.storage_locations[sl_name]
        if sl.storage_type == StorageType.LOCAL:
            continue
        entries = await rclone_lsjson(
            config.rclone_config_path, source=sl_name,
            source_path=sl.store_path / const.REMOTE_BOXES_REL_PATH,
            files_only=True, recursive=True, max_depth=3,
            filter=[
                f"+ /*/{const.BOX_METAFILE_REL_PATH}",
                f"+ /*/{const.BOX_SNAPSHOT_POINTER_REL_PATH}",
                f"+ /*/{const.BOX_CONF_REL_PATH}/**",
                "- **",
            ],
        ) or []
        for e in entries:
            parts = Path(e["Path"]).parts
            if len(parts) == 3 and parts[1] == const.BOX_CONF_REL_PATH:
                conf_dirs.add(parts[0])
            elif len(parts) == 2 and parts[1] == const.BOX_SNAPSHOT_POINTER_REL_PATH:
                pointer[parts[0]] = (e.get("ModTime"), e.get("Size"))
        record_entries += await rclone_lsjson(
            config.rclone_config_path, source=sl_name,
            source_path=sl.store_path / const.SYNC_RECORDS_REL_PATH,
            files_only=True, recursive=True, max_depth=2,
            filter=["+ /*/*.rec", "+ /*/*.rec.*", "- **"],
        ) or []
    return pointer, conf_dirs, project_record_listing(record_entries)


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


async def judge_box(config, bm, pointer, conf_dirs, live_view, tombstoned, sem):
    """One box: real statuses, synthesized view, verdict, comparison."""
    from boxyard._checkout import LocalCheckoutState, get_box_checkout_status

    row = {"box": bm.index_name, "format": bm.storage_format.value, "parts": {}, "errors": {}}
    checkout = get_box_checkout_status(config, bm).state
    row["checkout"] = checkout.value
    statuses = {}
    for part in BoxPart:
        if part is BoxPart.DATA and (
            checkout is not LocalCheckoutState.INCLUDED or bm.storage_format is StorageFormat.RESTIC
        ):
            continue  # the real path raises / restic has its own filter; nothing to compare
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
            "remote_path_exists": st.remote_path_exists,
        }

    # The view the listing will show after bootstrap: a marker per remote record.
    synth = RemoteRecordView()
    for part, st in statuses.items():
        if st.remote_sync_record is not None:
            synth.records.add(part.value)
            synth.markers[part.value] = [str(st.remote_sync_record.ulid)]
    live = live_view.get(bm.index_name)
    if live is not None:  # parts the oracle did not probe keep what the listing says
        for p in live.records - synth.records:
            synth.records.add(p)
        for p, ulids in live.markers.items():
            synth.markers.setdefault(p, ulids)

    verdict = boxes_needing_sync_full(
        config, [bm], requested_parts=list(BoxPart),
        record_views={bm.index_name: synth}, pointer_listing=pointer,
        remote_conf_dirs=conf_dirs, tombstoned=tombstoned, skip_meta=True, skip_data=True,
    )
    row["skippable"] = bm.index_name in verdict.skippable
    row["reason"] = verdict.reasons.get(bm.index_name)

    # Also the verdict from the LIVE listing (no synthesized markers) -- what a
    # pass run today would do before any bootstrap.
    live_verdict = boxes_needing_sync_full(
        config, [bm], requested_parts=list(BoxPart),
        record_views=live_view, pointer_listing=pointer,
        remote_conf_dirs=conf_dirs, tombstoned=tombstoned, skip_meta=True, skip_data=True,
    )
    row["skippable_today"] = bm.index_name in live_verdict.skippable

    # The comparison that matters.
    if row["skippable"]:
        bad = {p.value: st.sync_condition.value for p, st in statuses.items() if st.sync_condition not in ACCEPTABLE}
        row["wrong_skip"] = bad or None
    else:
        row["wrong_skip"] = None
        row["missed_skip"] = all(st.sync_condition in ACCEPTABLE for st in statuses.values()) and not row["errors"]

    # Cross-check: the listing's conf-dir signal against the real path's view.
    conf = statuses.get(BoxPart.CONF)
    if conf is not None:
        row["conf_dir_listing"] = bm.index_name in conf_dirs
        row["conf_dir_real"] = conf.remote_path_exists
    return row


async def main(argv):
    offset = int(argv[argv.index("--offset") + 1]) if "--offset" in argv else 0
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None
    concurrency = int(argv[argv.index("--concurrency") + 1]) if "--concurrency" in argv else 3

    config = get_config(Path.home() / ".config" / "boxyard" / "config.toml")
    box_metas = sorted(get_boxyard_meta(config).box_metas, key=lambda b: b.index_name)
    chunk = box_metas[offset: offset + limit if limit else None]
    t0 = time.time()
    pointer, conf_dirs, live_view = await bulk_listings(config, box_metas)
    tombstoned = set()
    for sl_name in sorted({bm.storage_location for bm in box_metas}):
        if config.storage_locations[sl_name].storage_type != StorageType.LOCAL:
            ids = await list_tombstoned_box_ids(config, sl_name)
            tombstoned |= {bm.index_name for bm in box_metas if bm.storage_location == sl_name and bm.box_id in ids}
    print(f"listings: {time.time() - t0:.1f}s; {len(live_view)} boxes with records, "
          f"{len(conf_dirs)} with remote conf files, {len(pointer)} pointers, {len(tombstoned)} tombstoned",
          file=sys.stderr)

    sem = asyncio.Semaphore(concurrency)
    rows = await asyncio.gather(*(judge_box(config, bm, pointer, conf_dirs, live_view, tombstoned, sem) for bm in chunk))
    if out:
        with out.open("a") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    wrong = [r for r in rows if r["wrong_skip"]]
    print(f"{len(chunk)} boxes in {time.time() - t0:.0f}s: {sum(r['skippable'] for r in rows)} skippable after bootstrap, "
          f"{sum(r['skippable_today'] for r in rows)} skippable today, {len(wrong)} WRONG SKIPS", file=sys.stderr)
    for r in wrong:
        print(f"WRONG SKIP: {r['box']} {r['wrong_skip']}", file=sys.stderr)
    return 1 if wrong else 0


def summary(path: Path):
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    print(f"{len(rows)} boxes judged")
    print(f"  skippable after bootstrap: {sum(r['skippable'] for r in rows)}")
    print(f"  skippable today (no markers yet): {sum(r['skippable_today'] for r in rows)}")
    print(f"  WRONG skips: {sum(1 for r in rows if r['wrong_skip'])}")
    for r in rows:
        if r["wrong_skip"]:
            print(f"    {r['box']}: {r['wrong_skip']}")
    needed = [r for r in rows if not r["skippable"]]
    print(f"  needed: {len(needed)}; first unprovable part: {dict(Counter(r['reason'] for r in needed))}")
    missed = [r for r in needed if r.get("missed_skip")]
    print(f"  missed skips (needed, yet every probed part SYNCED/EXCLUDED): {len(missed)}")
    print(f"    by reason: {dict(Counter(r['reason'] for r in missed))}")
    print(f"    by checkout: {dict(Counter(r['checkout'] for r in missed))}")
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

# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Doctor: `diverged-box` Integration Test
#
# Until this check existed, `boxyard doctor` could not see a wedged box at all.
# Two boxes on macbook sat wedged from March to August 2026 while doctor
# reported "all checks passed" on that machine throughout; they surfaced only in
# the supervisor log, one line per 20-minute pass, for five months.
#
# **On staging records directly.** Each case below writes the sync records it
# needs rather than racing two boxyards into a real conflict. That is deliberate
# and it is what makes these tests meaningful: a real conflict develops over
# hours or days, and doctor's prefilter (which skips a remote record written at
# the same moment as the local one) is only exercised honestly if the records
# carry realistic separation. Two boxyards driven a few seconds apart would test
# the opposite of the production case.

# %%
#|default_exp integration.cmds.test_doctor_diverged
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
import os
import pytest
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone

from ulid import ULID

from boxyard import const
from boxyard.cmds import new_box, sync_box, sync_missing_boxmetas, include_box
from boxyard.cmds._doctor import run_doctor
from boxyard._models import get_boxyard_meta, BoxPart, SyncRecord

from tests.integration.conftest import create_boxyards

# %%
#|top_export
@pytest.mark.integration
def test_doctor_diverged_box():
    """Doctor reports wedged boxes, and only wedged boxes."""
    asyncio.run(_test_doctor_diverged_box())

# %%
#|set_func_signature
async def _test_doctor_diverged_box(): ...

# %% [markdown]
# ## Two boxyards sharing one remote

# %%
#|export
(
    sl_name,
    sl_rclone_path,
    [(config1, config_path1, data_path1), (config2, config_path2, data_path2)],
) = create_boxyards(num_boxyards=2)

index_name = new_box(
    config_path=config_path1, box_name="wedge-me", storage_location=sl_name
)
await sync_box(config_path=config_path1, box_index_name=index_name)
await sync_missing_boxmetas(config_path=config_path2)
await include_box(config_path=config_path2, box_index_name=index_name)

box_meta = get_boxyard_meta(config2, force_create=True).by_index_name[index_name]
data_path = box_meta.get_local_part_path(config2, BoxPart.DATA)
local_rec_path = box_meta.get_local_sync_record_path(config2, BoxPart.DATA)

# The test remote is an rclone alias onto a local directory, so its records can
# be reached as plain paths; `store_path` is where the store sits inside it.
remote_rec_path = (
    Path(sl_rclone_path)
    / config2.storage_locations[sl_name].store_path
    / const.SYNC_RECORDS_REL_PATH
    / index_name
    / f"{BoxPart.DATA.value}.rec"
)
assert remote_rec_path.exists(), f"expected a pushed record at {remote_rec_path}"

# %% [markdown]
# ## Helpers
#
# `age` back-dates the local record; `touch_box` decides whether the local copy
# looks modified since it. Together they express each case in the same two terms
# doctor reasons in.

# %%
#|export
NOW = datetime.now(timezone.utc)


def set_local_record(*, days_ago: float, sync_complete: bool = True, host: str = "macbook"):
    ts = NOW - timedelta(days=days_ago)
    local_rec_path.write_text(
        SyncRecord(
            ulid=ULID.from_datetime(ts), sync_complete=sync_complete, syncer_hostname=host
        ).model_dump_json()
    )


def set_remote_record(*, days_ago: float, sync_complete: bool = True, host: str = "macstudio"):
    ts = NOW - timedelta(days=days_ago)
    remote_rec_path.write_text(
        SyncRecord(
            ulid=ULID.from_datetime(ts), sync_complete=sync_complete, syncer_hostname=host
        ).model_dump_json()
    )


def touch_box(*, days_ago: float):
    """Set every file in the box's DATA part to one age."""
    when = (NOW - timedelta(days=days_ago)).timestamp()
    for p in [data_path, *data_path.rglob("*")]:
        os.utime(p, (when, when))


async def findings():
    report = await run_doctor(config_path=config_path2)
    return report, [
        f for f in report["checks"]["diverged-box"]["findings"]
        if f.get("index_name") == index_name
    ]

# %% [markdown]
# ## A healthy box is not reported

# %%
#|export
_, found = await findings()
assert not found, f"a freshly synced box was reported as wedged: {found}"

# %% [markdown]
# ## A box that merely needs pulling is not reported
#
# Its records disagree too, so the naive comparison fires on it. It must not: on
# a fleet where most boxes are routinely a sync behind, that would flag hundreds
# of healthy boxes and make the whole report worthless.

# %%
#|export
set_local_record(days_ago=2)
set_remote_record(days_ago=1)  # remote moved ahead
touch_box(days_ago=3)  # ...and nothing changed here since our own record

_, found = await findings()
assert not found, f"a box that merely needs pulling was reported as wedged: {found}"

# %% [markdown]
# ## Both sides moved on: a divergence
#
# Same records as above — the only difference is that the local copy has also
# changed since its own record. That single fact is what turns a pull into a
# conflict, and doctor decides it with the sync engine's own exclude-aware scan.

# %%
#|export
touch_box(days_ago=0)

_, found = await findings()
assert found, "a diverged box went unreported"
assert "diverged" in found[0]["message"], f"unexpected wording: {found[0]}"
assert "macstudio" in found[0]["message"] and "macbook" in found[0]["message"], (
    f"the finding must name both sides so the user knows where to look: {found[0]}"
)
assert "box-status" in found[0]["hint"], (
    f"the hint must say how to inspect before choosing a side: {found[0]}"
)

# %% [markdown]
# ## Debris that is never synced does not count as a local change
#
# The same rule the sync engine follows: a file the filters exclude cannot make
# a box look modified. Without this, OS debris alone would produce a permanent
# false conflict — the exact mechanism behind the box wedged since March.

# %%
#|export
touch_box(days_ago=3)
_ds_store = data_path / ".DS_Store"
_ds_store.write_text("junk")
assert ".DS_Store" in config2.default_rclone_exclude_path.read_text()

_, found = await findings()
assert not found, f"excluded debris was treated as a local change: {found}"
_ds_store.unlink()

# %% [markdown]
# ## A push from another machine that never completed
#
# The local record is complete, the remote one is not: a push died half-way.
# Nothing could see this before — `interrupted-sync` reads only local records —
# so the box simply refused to sync, in silence, indefinitely.

# %%
#|export
set_local_record(days_ago=2)
set_remote_record(days_ago=1, sync_complete=False)
touch_box(days_ago=3)

_, found = await findings()
assert found, "an interrupted push from another machine went unreported"
assert "never completed" in found[0]["message"], (
    "this must be reported as a half-written push, not as a divergence -- the two "
    f"are resolved differently: {found[0]}"
)
assert "macstudio" in found[0]["message"], (
    f"the finding must name the machine that left it half-written: {found[0]}"
)

# %% [markdown]
# ## A push still in flight is not reported
#
# It is indistinguishable from an interrupted one, so only a remote record older
# than the grace window counts. Reporting live pushes would flag every long sync
# on the fleet, and a check that cries wolf is one nobody reads.

# %%
#|export
set_remote_record(days_ago=0.01, sync_complete=False)  # ~15 minutes in

_, found = await findings()
assert not found, f"a push that started minutes ago was reported as wedged: {found}"

# %% [markdown]
# ## An interrupted sync on THIS machine stays `interrupted-sync`'s business
#
# One underlying problem must produce one finding. Reporting it under two check
# names would have the user chase the same box twice, by two different routes.

# %%
#|export
set_local_record(days_ago=2, sync_complete=False)
set_remote_record(days_ago=1)
touch_box(days_ago=0)  # would otherwise read as a divergence

report, found = await findings()
assert not found, f"an interrupted local sync was also reported as a divergence: {found}"
assert [
    f for f in report["checks"]["interrupted-sync"]["findings"]
    if f.get("index_name") == index_name
], "...and it must still be reported by interrupted-sync"

# %% [markdown]
# ## Offline, the check reports SKIPPED rather than a false all-clear

# %%
#|export
offline = await run_doctor(config_path=config_path2, check_remote=False)
assert offline["checks"]["diverged-box"]["skipped"] is True, (
    "with no remote access the check must report SKIPPED, never 'ok'"
)

# %%
print("diverged-box OK")
# %% [markdown]
# ## META divergence is decided by the fingerprint too — under the signature META is WRITTEN with
#
# Only DATA syncs under an exclude file; META and CONF baselines are written by
# `sync_helper` under `filter_signature(None)`. Doctor once read every part
# under the DATA exclude's signature, so META/CONF baselines could never match
# and those parts sat on the mtime fallback for ever — exactly the "doctor and
# sync disagree" this check's own comment promises cannot happen.
#
# The scenario is built so the two predicates DISAGREE: the local boxmeta is
# edited but its mtime backdated behind the local record. The fallback calls
# that unchanged (needs-pull, silent); the fingerprint sees the edit (conflict,
# reported). Only a doctor that actually reads the baseline reports it.

# %%
#|export
# Reset the wreckage of the previous sections with a real sync.
await sync_box(config_path=config_path2, box_index_name=index_name)

from boxyard._fingerprint import filter_signature, tree_fingerprint, write_base

meta_path = box_meta.get_local_part_path(config2, BoxPart.META)
meta_rec_path = box_meta.get_local_sync_record_path(config2, BoxPart.META)
remote_meta_rec_path = remote_rec_path.with_name(f"{BoxPart.META.value}.rec")

_local_ulid = ULID.from_datetime(NOW - timedelta(days=2))
meta_rec_path.write_text(
    SyncRecord(ulid=_local_ulid, sync_complete=True, syncer_hostname="macbook").model_dump_json()
)
remote_meta_rec_path.write_text(
    SyncRecord(
        ulid=ULID.from_datetime(NOW - timedelta(days=1)),  # remote moved ahead
        sync_complete=True,
        syncer_hostname="macstudio",
    ).model_dump_json()
)

# A usable baseline for the CURRENT boxmeta, bound to the local record, written
# exactly as `sync_helper` writes it for META: no exclude file, sig(None).
_meta_sig = filter_signature(None)
write_base(
    meta_rec_path,
    sync_record_ulid=str(_local_ulid),
    fingerprint=tree_fingerprint(meta_path, set(), filter_sig=_meta_sig),
    filter_sig=_meta_sig,
)

# The edit the mtime fallback cannot see: content changes, mtime backdated
# behind the local record.
meta_path.write_text(meta_path.read_text() + "\n# edited on this machine\n")
_when = (NOW - timedelta(days=3)).timestamp()
os.utime(meta_path, (_when, _when))

report, _ = await findings()
_meta_found = [
    f for f in report["checks"]["diverged-box"]["findings"]
    if f.get("index_name") == index_name and f.get("box_part") == "meta"
]
assert _meta_found, (
    "an edited META with a backdated mtime went unreported: doctor is not "
    "reading the META baseline under the signature META is written with"
)

# %%
# %% [markdown]
# ## The migration gauge rides along in every report, and never turns it red
#
# The `TODO(cleanup)` fallbacks can only be retired once this reads 0 uncovered
# on every machine, so the number has to exist somewhere observable — and it
# must be a gauge, not a finding: a missing baseline is documented migration
# state, not a fault, and a doctor that stays red for months is one nobody reads.

# %%
#|export
_cov = report["fingerprint_baseline_coverage"]
assert set(_cov) == {"parts_covered", "parts_uncovered", "uncovered"}
assert _cov["parts_covered"] >= 1, (
    "the constructed META baseline is bound to the current record under the "
    "reader's signature, so it must count as covered"
)
assert _cov["parts_uncovered"] == len(_cov["uncovered"])

# %%

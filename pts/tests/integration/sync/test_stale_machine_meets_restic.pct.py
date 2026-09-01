# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # A stale machine meets a box that was NEVER plain
#
# With `restic` as the default, this stops being a migration-only concern and
# becomes the normal path for every new box. And it is a DIFFERENT case from a
# converted box.
#
# A CONVERTED box has history on the stale machine: a local checkout and a local
# `data.rec`, and the remote's `data/` and `data.rec` are gone. Those absences
# drive `get_sync_status` into its ERROR branch, so the stale machine refuses
# loudly. That is tested in `test_convert_box.py`.
#
# A box that was NEVER plain gives the stale machine none of that. It discovers
# the box through `sync-missing-meta` like any other, and then finds NEITHER a
# local checkout NOR a remote `data/` — which is the shape of a box that is
# simply not included here. There is nothing for it to refuse.
#
# These tests establish what actually happens, so the release note can say it
# rather than guess. The "stale machine" is boxyard with the restic branch
# disabled — i.e. what 0.6.1 is.

# %%
#|default_exp integration.sync.test_stale_machine_meets_restic

# %%
#|export
import asyncio
import shutil
import tomllib
from pathlib import Path

import tomli_w

import pytest

from boxyard import const
from boxyard._enums import BoxPart, StorageFormat
from boxyard._models import BoxMeta, SyncCondition, get_boxyard_meta
from boxyard.cmds import include_box, new_box, sync_box, sync_missing_boxmetas
from boxyard.config import get_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("restic") is None, reason="restic binary not available"
    ),
]


def run(coro):
    return asyncio.run(coro)


# %%
#|export
@pytest.fixture
def fresh_restic_box(monkeypatch, tmp_path):
    """
    Machine A (upgraded) creates a box that is restic from birth and pushes it.
    Machine B stands for a stale one: it has never seen the box.
    """
    from tests.integration.conftest import create_boxyards

    monkeypatch.setenv("BOXYARD_RESTIC_PASSWORD", "stale-test-password")
    for target in ("boxyard.const", "boxyard._restic.const"):
        monkeypatch.setattr(f"{target}.RESTIC_CANONICAL_ROOT", str(tmp_path / "canon"))

    remote_name, remote_root, yards = create_boxyards(num_boxyards=2)
    (cfgA, cpA, _), (cfgB, cpB, _) = yards

    # The shared fixtures pin `plain` so the rest of the suite keeps exercising
    # the path all 596 existing boxes use. A test that wants the new default
    # asks for it, which is also what a real yard's config will do.
    for _cp in (cpA, cpB):
        _dump = tomllib.loads(Path(_cp).read_text())
        _dump.setdefault("sync_policies", {}).setdefault(
            "default", {}
        )["storage_format"] = "restic"
        Path(_cp).write_text(tomli_w.dumps(_dump))
    cfgA, cfgB = get_config(cpA), get_config(cpB)

    idx = new_box(config_path=cpA, box_name="borndigital",
                  storage_location=remote_name, claim=False)
    dataA = get_boxyard_meta(cfgA).by_index_name[idx].get_local_part_path(
        cfgA, BoxPart.DATA
    )
    (dataA / "notes.md").write_text("born restic\n")
    run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))

    return {
        "idx": idx, "cfgA": cfgA, "cpA": cpA, "cfgB": cfgB, "cpB": cpB,
        "dataA": dataA, "remote_name": remote_name,
        "box_root": remote_root / "boxyard" / const.REMOTE_BOXES_REL_PATH / idx,
    }


class _BuildWithoutRestic:
    """A `StorageFormat` whose RESTIC no real boxmeta can ever equal."""

    RESTIC = object()


def stale(monkeypatch):
    """
    Make this process behave like a build that does not know the format.

    Swaps the `StorageFormat` NAME that `sync_box` compares against, so the
    dispatch never matches and the plain branch is taken -- which is precisely
    what 0.6.1 does, since its `BoxMeta` has no `storage_format` field and the
    key lands in `unknown_keys` instead.

    Deliberately NOT done by stripping the key from the local boxmeta: a real
    0.6.1 machine PRESERVES it through `unknown_keys` and writes it back, so
    stripping would model a machine that also corrupts the shared boxmeta and
    would make these tests pessimistic in a misleading way.
    """
    import boxyard.cmds._sync_box as mod

    monkeypatch.setattr(mod, "StorageFormat", _BuildWithoutRestic)


# %% [markdown]
# ## What an upgraded machine produces

# %%
#|export
def test_a_new_box_is_restic_from_birth(fresh_restic_box):
    config = get_config(fresh_restic_box["cpA"])
    meta = BoxMeta.load(config, fresh_restic_box["remote_name"],
                        fresh_restic_box["idx"])
    assert meta.storage_format is StorageFormat.RESTIC
    assert (fresh_restic_box["box_root"] / const.BOX_RESTIC_REL_PATH).is_dir()
    assert (fresh_restic_box["box_root"] / const.BOX_SNAPSHOT_POINTER_REL_PATH).is_file()
    assert not (fresh_restic_box["box_root"] / const.BOX_DATA_REL_PATH).exists()


def test_its_first_sync_creates_the_repository(fresh_restic_box):
    """
    `new_box` does not touch the remote, so a restic box's repository is created
    by its FIRST sync -- exactly as a plain box's remote `data/` is.
    """
    results = run(sync_box(config_path=fresh_restic_box["cpA"],
                           box_index_name=fresh_restic_box["idx"], verbose=False))
    assert results[BoxPart.DATA][0].sync_condition is SyncCondition.SYNCED


# %% [markdown]
# ## What a STALE machine does with it
#
# The answer is the point of these tests, and it is not a refusal.

# %%
#|export
def test_a_stale_machine_discovers_it_like_any_other_box(fresh_restic_box, monkeypatch):
    """
    `sync-missing-meta` has no notion of storage format, so the box registers on
    the stale machine and appears in `boxyard list` and the group symlinks.
    """
    run(sync_missing_boxmetas(config_path=fresh_restic_box["cpB"], verbose=False))
    config = get_config(fresh_restic_box["cpB"])
    assert fresh_restic_box["idx"] in get_boxyard_meta(config).by_index_name


def test_a_stale_machine_reports_synced_and_does_nothing(fresh_restic_box, monkeypatch):
    """
    THE FINDING. A never-plain box gives a stale machine no local checkout and
    no remote `data/`, which is the shape of a box that is simply not included
    here -- so it reports SYNCED and does nothing. It does NOT refuse, because
    there is nothing to refuse: unlike a CONVERTED box, there are no leftovers
    to contradict.

    Not destructive, but SILENT, which is why the release order matters more
    than any code gate could.
    """
    stale(monkeypatch)
    run(sync_missing_boxmetas(config_path=fresh_restic_box["cpB"], verbose=False))

    results = run(sync_box(config_path=fresh_restic_box["cpB"],
                           box_index_name=fresh_restic_box["idx"], verbose=False))

    assert results[BoxPart.DATA][0].sync_condition is SyncCondition.SYNCED
    assert results[BoxPart.DATA][1] is False


def test_a_stale_machine_does_not_destroy_anything(fresh_restic_box, monkeypatch):
    """The property that has to hold whatever else does."""
    stale(monkeypatch)
    run(sync_missing_boxmetas(config_path=fresh_restic_box["cpB"], verbose=False))
    run(sync_box(config_path=fresh_restic_box["cpB"],
                 box_index_name=fresh_restic_box["idx"], verbose=False))

    assert (fresh_restic_box["box_root"] / const.BOX_RESTIC_REL_PATH).is_dir()
    assert (fresh_restic_box["box_root"] / const.BOX_SNAPSHOT_POINTER_REL_PATH).is_file()
    assert not (fresh_restic_box["box_root"] / const.BOX_DATA_REL_PATH).exists()
    assert (fresh_restic_box["dataA"] / "notes.md").read_text() == "born restic\n"


def test_a_stale_machine_cannot_check_the_box_out(fresh_restic_box, monkeypatch):
    """
    The outcome that closes the divergence hazard, and it is a refusal.

    `include` pulls the remote `data/`, which a never-plain box does not have,
    so rclone fails and the checkout is never created. That matters: without a
    local checkout the stale machine can never push a plain `data/` beside the
    repository, which is the one way it could have made the box exist in two
    formats at once.

    It refuses for the right reason and says it badly -- `SyncFailed` carries
    rclone's empty output and never mentions the format. Recorded here rather
    than fixed: the fix belongs in a build that understands `storage_format`,
    and by definition the machine hitting this does not have one. The real
    remedy is the release order.
    """
    from boxyard._utils.sync_helper import SyncFailed

    stale(monkeypatch)
    run(sync_missing_boxmetas(config_path=fresh_restic_box["cpB"], verbose=False))

    with pytest.raises((SyncFailed, Exception)) as excinfo:
        run(include_box(config_path=fresh_restic_box["cpB"],
                        box_index_name=fresh_restic_box["idx"], read_only=True))
    assert excinfo.type is not AssertionError

    config = get_config(fresh_restic_box["cpB"])
    dataB = get_boxyard_meta(config).by_index_name[
        fresh_restic_box["idx"]
    ].get_local_part_path(config, BoxPart.DATA)
    contents = sorted(p.name for p in dataB.rglob("*")) if dataB.is_dir() else []
    assert contents == [], (
        f"a refused include left content behind: {contents}"
    )


def test_doctor_on_a_stale_machine_names_the_unknown_key(
    fresh_restic_box, monkeypatch
):
    """
    The one signal a stale machine DOES get. `storage_format` is a key its
    `BoxMeta` does not know, so it lands in `unknown_keys` and doctor reports
    `unknown-boxmeta-keys` -- which is the thread a person pulls to discover
    they are behind.
    """
    from boxyard.cmds import run_doctor

    run(sync_missing_boxmetas(config_path=fresh_restic_box["cpB"], verbose=False))

    config = get_config(fresh_restic_box["cpB"])
    meta_path = (
        config.local_store_path / fresh_restic_box["remote_name"]
        / fresh_restic_box["idx"] / const.BOX_METAFILE_REL_PATH
    )
    assert "storage_format" in meta_path.read_text(), (
        "the boxmeta must carry the format, or a stale machine gets no signal"
    )

    report = run(run_doctor(config_path=fresh_restic_box["cpB"], check_remote=False))
    assert "unknown-boxmeta-keys" in report["checks"]


# %% [markdown]
# ## The password is a precondition of CREATION, not a surprise at sync time
#
# A machine that upgrades without `restic_password_command` configured would
# otherwise create boxes it cannot push, and only find out on the first sync.

# %%
#|export
def test_creating_a_restic_box_without_a_password_is_refused(
    fresh_restic_box, monkeypatch
):
    """
    Fails at the only moment a person can act on it, and names the key.

    Deliberately NOT a silent fallback to `plain`: that would make a box's
    format depend on whether config happened to be present, so two machines
    could create two different formats for one yard and nothing would say so.
    """
    monkeypatch.delenv("BOXYARD_RESTIC_PASSWORD", raising=False)
    import boxyard._restic as restic_mod

    monkeypatch.setattr(restic_mod, "_password_cache", {})

    with pytest.raises(ValueError) as excinfo:
        new_box(config_path=fresh_restic_box["cpA"], box_name="nopassword",
                storage_location=fresh_restic_box["remote_name"], claim=False)

    message = str(excinfo.value)
    assert "restic_password_command" in message
    assert 'storage_format = "plain"' in message, (
        "the refusal must name both ways out, not just one"
    )


def test_creating_a_plain_box_needs_no_password(fresh_restic_box, monkeypatch):
    """The escape hatch the refusal names has to actually work."""
    monkeypatch.delenv("BOXYARD_RESTIC_PASSWORD", raising=False)
    import boxyard._restic as restic_mod

    monkeypatch.setattr(restic_mod, "_password_cache", {})

    cp = fresh_restic_box["cpA"]
    dump = tomllib.loads(Path(cp).read_text())
    dump["sync_policies"]["default"]["storage_format"] = "plain"
    Path(cp).write_text(tomli_w.dumps(dump))

    idx = new_box(config_path=cp, box_name="plainstillworks",
                  storage_location=fresh_restic_box["remote_name"], claim=False)
    config = get_config(cp)
    assert BoxMeta.load(
        config, fresh_restic_box["remote_name"], idx
    ).storage_format is StorageFormat.PLAIN

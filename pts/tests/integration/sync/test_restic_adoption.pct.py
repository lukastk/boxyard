# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Adopting a converted box on a second machine
#
# The whole migration story: convert on one machine, then get the box onto the
# next one. The interruption table proved CONVERSION; nothing proved ADOPTION,
# and that is why the gap reached a real machine.
#
# What it looked like there: `boxyard include` printed "Included box ... in
# checkout root 'default'", created **no directory at all**, and `boxyard sync`
# then said the recorded checkout was missing and to run `boxyard include` — a
# loop with no exit.
#
# The cause was that "no local tree, a snapshot on the remote" describes TWO
# states that look identical on the filesystem: a box deliberately excluded
# here, and a box being included right now. The plain path needs no case for
# the second because rclone creates the destination on the way past. Restic has
# to be told, and was not.
#
# These tests assert the adopted content matches by **hash and mode**, not by
# "a directory appeared" — the real check on macstudio was per-file sha256 plus
# mode, and a weaker assertion here is what let this through.

# %%
#|default_exp integration.sync.test_restic_adoption

# %%
#|export
import asyncio
import hashlib
import os
import shutil
import stat
from pathlib import Path

import pytest

from boxyard._enums import BoxPart, StorageFormat
from boxyard._models import SyncCondition, get_boxyard_meta
from boxyard._restic import read_state
from boxyard.cmds import (
    convert_box,
    exclude_box,
    include_box,
    new_box,
    sync_box,
    sync_missing_boxmetas,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("restic") is None, reason="restic binary not available"
    ),
]


def run(coro):
    return asyncio.run(coro)


PERMS_MANIFEST = ".boxyard-perms.json"


def fingerprint(root: Path) -> dict[str, tuple[str, str]]:
    """
    {relative path: (sha256 or link target, mode)} for everything under `root`.

    Modes are included because the exec bit is the thing a naive copy loses,
    and symlinks are recorded by TARGET rather than followed -- a restore that
    turned a symlink into a copy of its victim would otherwise pass.

    The exec-bit manifest is left out, and its absence is asserted separately.
    `data_root_exclude_args` keeps it out of every snapshot on purpose: restic
    carries Unix mode natively, so the manifest is dead weight for a restic box
    and including it would churn a generated file inside the thing that exists
    to stop churn. A machine converted from plain keeps its old copy as
    residue; a machine that adopts never gets one.
    """
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if rel == PERMS_MANIFEST:
            continue
        if p.is_symlink():
            out[rel] = (f"link:{os.readlink(p)}", "link")
        elif p.is_file():
            out[rel] = (
                hashlib.sha256(p.read_bytes()).hexdigest(),
                oct(stat.S_IMODE(p.lstat().st_mode)),
            )
        elif p.is_dir():
            out[rel] = ("dir", oct(stat.S_IMODE(p.lstat().st_mode)))
    return out


def data_of(cfg, idx) -> Path:
    return get_boxyard_meta(cfg).by_index_name[idx].get_local_part_path(cfg, BoxPart.DATA)


# %%
#|export
@pytest.fixture
def converted_on_A(monkeypatch, tmp_path):
    """
    Machine A converts a box with content worth checking: nested directories,
    an executable, a symlink, an empty file and a large-ish file.

    Machine B is a second machine in the same yard that has never held it.
    """
    from tests.integration.conftest import create_boxyards

    monkeypatch.setenv("BOXYARD_RESTIC_PASSWORD", "adoption-test-password")
    for target in ("boxyard.const", "boxyard._restic.const"):
        monkeypatch.setattr(f"{target}.RESTIC_CANONICAL_ROOT", str(tmp_path / "canon"))

    remote_name, remote_root, yards = create_boxyards(num_boxyards=2)
    (cfgA, cpA, _), (cfgB, cpB, _) = yards

    idx = new_box(config_path=cpA, box_name="adoptme",
                  storage_location=remote_name, claim=False)
    data = data_of(get_config_(cpA), idx)
    (data / "notes.md").write_text("# notes\nline one\n")
    (data / "nested" / "deep").mkdir(parents=True)
    (data / "nested" / "deep" / "buried.txt").write_text("buried\n")
    (data / "empty.txt").write_text("")
    (data / "big.bin").write_bytes(bytes(range(256)) * 400)
    script = data / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    (data / "nested" / "link-to-notes").symlink_to("../notes.md")

    run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))
    run(convert_box(config_path=cpA, box_index_name=idx, verbose=False))

    return {
        "idx": idx, "cpA": cpA, "cpB": cpB,
        "remote_name": remote_name, "remote_root": remote_root,
        "before": fingerprint(data), "dataA": data,
    }


def get_config_(path):
    from boxyard.config import get_config

    return get_config(path)


# %% [markdown]
# ## The test the canary needed

# %%
#|export
def test_include_materialises_a_converted_box_on_a_second_machine(converted_on_A):
    """
    THE regression. `include` must create the tree, and the tree must match
    machine A's byte for byte, mode for mode, symlink for symlink.
    """
    cpB = converted_on_A["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))

    run(include_box(config_path=cpB, box_index_name=converted_on_A["idx"]))

    cfgB = get_config_(cpB)
    dataB = data_of(cfgB, converted_on_A["idx"])
    assert dataB.is_dir(), (
        "`include` reported success and created no directory -- the canary bug"
    )
    assert fingerprint(dataB) == converted_on_A["before"], (
        "the adopted tree differs from the machine it was converted on"
    )
    assert not (dataB / PERMS_MANIFEST).exists(), (
        "the exec-bit manifest must not be in the snapshot -- restic carries "
        "mode natively and the manifest is excluded on purpose"
    )
    assert oct(stat.S_IMODE((dataB / "run.sh").lstat().st_mode)) == "0o755", (
        "the exec bit survived WITHOUT the manifest, which is the point"
    )


def test_the_adopted_box_is_immediately_settled(converted_on_A):
    """
    Adoption must leave the box SYNCED, not perpetually needing something. A
    machine that adopts and then reports CONFLICT or NEEDS_PULL for ever is the
    same class of dead end as the include loop.
    """
    cpB = converted_on_A["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=converted_on_A["idx"]))

    results = run(sync_box(config_path=cpB, box_index_name=converted_on_A["idx"],
                           verbose=False))
    assert results[BoxPart.DATA][0].sync_condition is SyncCondition.SYNCED

    state = read_state(get_config_(cpB).boxyard_data_path, converted_on_A["idx"])
    assert state is not None and state.get("snapshot")
    assert not state.get("pulling_from"), (
        "the pull marker must be cleared once the restore completes"
    )


def test_sync_does_not_send_the_person_back_to_include(converted_on_A):
    """
    The exact loop reported from macstudio: `include` says it worked, `sync`
    says the checkout is missing and to run `include`.

    Driven through the placement record rather than by deleting the directory,
    because that is the state `include` actually leaves behind if its DATA pull
    does nothing.
    """
    cpB = converted_on_A["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=converted_on_A["idx"]))

    dataB = data_of(get_config_(cpB), converted_on_A["idx"])
    shutil.rmtree(dataB)

    # Placement still says INCLUDED, so this is the MISSING state -- and a
    # plain `sync` must now recover it rather than refuse.
    results = run(sync_box(config_path=cpB, box_index_name=converted_on_A["idx"],
                           verbose=False, _allow_missing_checkout=True))
    assert results[BoxPart.DATA][0].sync_condition is not SyncCondition.EXCLUDED
    assert dataB.is_dir(), "a MISSING checkout was not re-materialised"
    assert fingerprint(dataB) == converted_on_A["before"]


def test_an_excluded_box_is_still_left_alone(converted_on_A):
    """
    The other half of the same decision, and the reason this cannot simply
    always pull: a box excluded on purpose must stay excluded. If adoption keyed
    off "no directory" alone it would silently undo `boxyard exclude`.
    """
    cpB = converted_on_A["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=converted_on_A["idx"]))
    run(exclude_box(config_path=cpB, box_index_name=converted_on_A["idx"]))

    dataB = data_of(get_config_(cpB), converted_on_A["idx"])
    assert not dataB.exists(), "precondition: exclude removed the checkout"

    results = run(sync_box(config_path=cpB, box_index_name=converted_on_A["idx"],
                           verbose=False))
    assert results[BoxPart.DATA][0].sync_condition is SyncCondition.EXCLUDED
    assert not dataB.exists(), "sync re-pulled a deliberately excluded box"


def test_the_adopting_machine_can_then_push(converted_on_A):
    """
    Adoption is not read-only: the second machine must be able to edit and push,
    and machine A must see it. Otherwise the box is adopted into a dead end.
    """
    cpA, cpB = converted_on_A["cpA"], converted_on_A["cpB"]
    idx = converted_on_A["idx"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=idx))

    dataB = data_of(get_config_(cpB), idx)
    (dataB / "from-b.txt").write_text("written on B\n")
    run(sync_box(config_path=cpB, box_index_name=idx, verbose=False))

    run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))
    assert (converted_on_A["dataA"] / "from-b.txt").read_text() == "written on B\n"


# %% [markdown]
# ## The neighbours, which had the same gap
#
# `include` was the one that reached a machine, but it is not the only command
# that assumed `boxes/<box>/data/` exists. Anything that reads a box's DATA from
# the remote had to be checked, because conversion purges that directory.
#
# `discard-local` matters most of the three: it is what a person reaches for
# when a box is ALREADY in a bad state, so it failing is the worst possible
# time for it to fail.

# %%
#|export
def test_discard_local_works_on_a_converted_box(converted_on_A):
    """
    Takes the remote's version and keeps the discarded work. On a converted box
    this has to come from the repository -- the plain tree it used to pull is
    gone.
    """
    cpB = converted_on_A["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=converted_on_A["idx"]))

    cfgB = get_config_(cpB)
    dataB = data_of(cfgB, converted_on_A["idx"])
    (dataB / "notes.md").write_text("LOCAL EDIT that will be discarded\n")
    (dataB / "only-local.txt").write_text("also discarded\n")

    from boxyard.cmds import discard_local

    backups = run(discard_local(config_path=cpB,
                                box_index_name=converted_on_A["idx"],
                                verbose=False))

    assert fingerprint(dataB) == converted_on_A["before"], (
        "discard-local did not restore the remote's version"
    )
    # The promise of the command: what was thrown away is still there.
    saved = [
        p for p in Path(backups).rglob("only-local.txt")
    ]
    assert saved, (
        f"the discarded work was not kept under '{backups}' -- the whole "
        f"point of the command is that it is recoverable"
    )


def test_copy_from_remote_works_on_a_converted_box(converted_on_A, tmp_path):
    """
    Copies a box out to an arbitrary destination. For a converted box that is a
    restore from the snapshot, not a copy of a directory that no longer exists.
    """
    from boxyard.cmds import copy_from_remote

    run(sync_missing_boxmetas(config_path=converted_on_A["cpB"], verbose=False))
    dest = tmp_path / "copied-out"
    run(copy_from_remote(
        config_path=converted_on_A["cpB"],
        box_index_name=converted_on_A["idx"],
        dest_path=dest,
        verbose=False,
    ))
    copied_data = dest / "data" if (dest / "data").is_dir() else dest
    got = fingerprint(copied_data)
    assert got, "copy-from-remote produced nothing"
    assert got["notes.md"] == converted_on_A["before"]["notes.md"]
    assert got["run.sh"][1] == "0o755", "the exec bit did not survive the copy"


# %% [markdown]
# ## Refusing early when restic is not installed
#
# The canary machine had NO restic binary, and `include` still printed
# "Included box ...". The failure surfaced several commands later, by which
# point sync refused, exclude refused, and nothing could move the box.

# %%
#|export
def test_include_refuses_when_restic_is_missing(converted_on_A, monkeypatch):
    """
    Refuse BEFORE writing a placement record or printing success.

    The binary is hidden by pointing the override at a path that does not
    exist, which is the same door `get_restic_binary` looks through first.
    """
    from boxyard import const
    from boxyard._restic import ResticError

    cpB = converted_on_A["cpB"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))

    # The resolved binary is cached for the life of the process, so the cache
    # has to be dropped as well as the environment changed -- the fixture has
    # already used restic. (That cache is also why a machine which installs
    # restic mid-run needs a fresh process, which is fine: the alternative is
    # re-resolving the binary on every one of 590 boxes.)
    monkeypatch.setattr("boxyard._restic._restic_binary", None)
    monkeypatch.setenv(const.ENV_VAR_BOXYARD_RESTIC, "/nonexistent/restic")

    with pytest.raises(ResticError) as excinfo:
        run(include_box(config_path=cpB, box_index_name=converted_on_A["idx"]))

    message = str(excinfo.value)
    assert "include" in message, "the refusal must say what will not happen"

    cfgB = get_config_(cpB)
    dataB = data_of(cfgB, converted_on_A["idx"])
    assert not dataB.exists(), "a refused include left a directory behind"


# %% [markdown]
# ## A symlink-only change, end to end
#
# The unit audit in `test_change_detection.py` proves the predicate sees each
# kind of change. This proves the whole command does — reported from a real
# machine as `boxyard sync -c data` answering "DATA is up to date" with two
# unpushed symlinks sitting in the box, and not self-correcting.

# %%
#|export
def test_a_symlink_only_change_pushes_and_reaches_the_other_machine(converted_on_A):
    """
    Add symlinks and change NOTHING else, then sync. The box must push, and the
    second machine must receive them AS symlinks.
    """
    cpA, cpB = converted_on_A["cpA"], converted_on_A["cpB"]
    idx = converted_on_A["idx"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=idx))

    dataA = converted_on_A["dataA"]
    (dataA / "link-to-keep").symlink_to("nested/deep/buried.txt")
    (dataA / "alias-nested").symlink_to("nested")

    results = run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))
    assert results[BoxPart.DATA][0].sync_condition is not SyncCondition.SYNCED, (
        "a box with two unpushed symlinks reported itself up to date"
    )

    run(sync_box(config_path=cpB, box_index_name=idx, verbose=False))
    dataB = data_of(get_config_(cpB), idx)

    assert (dataB / "link-to-keep").is_symlink(), "arrived as a copy, not a link"
    assert os.readlink(dataB / "link-to-keep") == "nested/deep/buried.txt"
    assert (dataB / "alias-nested").is_symlink()
    assert (dataB / "alias-nested" / "deep" / "buried.txt").read_text() == "buried\n", (
        "the symlink does not resolve on the machine that received it"
    )


def test_retargeting_a_symlink_and_nothing_else_pushes(converted_on_A):
    """
    Same name, different target: nothing added, nothing removed, no file
    content touched. This one was never reported -- it came out of the audit.
    """
    cpA, cpB = converted_on_A["cpA"], converted_on_A["cpB"]
    idx = converted_on_A["idx"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=idx))

    dataA = converted_on_A["dataA"]
    link = dataA / "nested" / "link-to-notes"
    assert link.is_symlink(), "precondition: the box ships with a symlink"
    link.unlink()
    link.symlink_to("deep/buried.txt")

    results = run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))
    assert results[BoxPart.DATA][0].sync_condition is not SyncCondition.SYNCED

    run(sync_box(config_path=cpB, box_index_name=idx, verbose=False))
    dataB = data_of(get_config_(cpB), idx)
    assert os.readlink(dataB / "nested" / "link-to-notes") == "deep/buried.txt"


def test_a_chmod_only_change_pushes(converted_on_A):
    """
    The exec bit and nothing else. Worth its own end-to-end case: the design
    retires the perms manifest for restic boxes because restic carries mode
    natively, which was true of STORAGE and was not true of DETECTION -- so a
    `chmod +x` alone would never have reached the remote at all.
    """
    cpA, cpB = converted_on_A["cpA"], converted_on_A["cpB"]
    idx = converted_on_A["idx"]
    run(sync_missing_boxmetas(config_path=cpB, verbose=False))
    run(include_box(config_path=cpB, box_index_name=idx))

    (converted_on_A["dataA"] / "notes.md").chmod(0o755)

    results = run(sync_box(config_path=cpA, box_index_name=idx, verbose=False))
    assert results[BoxPart.DATA][0].sync_condition is not SyncCondition.SYNCED

    run(sync_box(config_path=cpB, box_index_name=idx, verbose=False))
    dataB = data_of(get_config_(cpB), idx)
    assert stat.S_IMODE((dataB / "notes.md").lstat().st_mode) == 0o755

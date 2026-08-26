# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # The META Merge Base
#
# A snapshot of the boxmeta as it stood at the last successful META sync, kept
# beside the local sync record and never synced anywhere.
#
# It exists because a boxmeta both sides have edited is currently a dead end:
# sync sees two records that disagree, cannot tell which fields moved on which
# side, and refuses. That is how 44 boxes on macbook stopped propagating their
# groups for a day in August 2026 while every other machine reported "all
# checks passed".
#
# The rule these tests exist to protect: **a stale base is worse than no base**.
# A merge computes what each side changed by diffing against it, so a base that
# never corresponded to a real shared state produces a confidently wrong
# answer, where a missing one only makes the merge decline.

# %%
#|default_exp unit.models.test_meta_merge_base

# %%
#|export
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from boxyard._models import BoxMeta, BoxPart, read_meta_base, record_meta_base

# %% [markdown]
# ## A box with somewhere to put its boxmeta

# %%
#|export
@pytest.fixture
def box(tmp_path, monkeypatch):
    """A BoxMeta whose META path and base path are both under tmp_path.

    Patched on the CLASS: BoxMeta is a pydantic model, so assigning a method
    onto an instance raises rather than shadowing.
    """
    meta_path = tmp_path / "store" / "20260822_aaaaa__a-box" / "boxmeta.toml"
    meta_path.parent.mkdir(parents=True)
    base_path = tmp_path / "records" / "20260822_aaaaa__a-box" / "meta.base.toml"

    bm = BoxMeta(
        creation_timestamp_utc="20260822_000000",
        box_subid="aaaaa",
        name="a-box",
        storage_location="remote",
        creator_hostname="test",
        groups=["work"],
    )
    monkeypatch.setattr(BoxMeta, "get_local_part_path", lambda self, cfg, part: meta_path)
    monkeypatch.setattr(BoxMeta, "get_local_meta_base_path", lambda self, cfg: base_path)
    return bm, MagicMock(), meta_path, base_path


# The shape of a real boxmeta.toml. `creator_hostname` is required by the
# model, and leaving it out would make every one of these tests exercise the
# reject path instead of the one they name.
BOXMETA = 'creator_hostname = "test"\nparents = []\ngroups = %s\n'


def write_meta(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")

# %% [markdown]
# ## Recording snapshots the file as it is

# %%
#|export
def test_record_writes_the_base(box):
    bm, config, meta_path, base_path = box
    write_meta(meta_path, BOXMETA % '["work"]')

    record_meta_base(config, bm)

    assert base_path.is_file()
    assert tomllib.loads(base_path.read_text())["groups"] == ["work"]


def test_recording_again_replaces_the_base(box):
    bm, config, meta_path, base_path = box
    write_meta(meta_path, BOXMETA % '["work"]')
    record_meta_base(config, bm)

    write_meta(meta_path, BOXMETA % '["work", "archived"]')
    record_meta_base(config, bm)

    assert tomllib.loads(base_path.read_text())["groups"] == ["work", "archived"]

# %% [markdown]
# ## A base is never left describing nothing

# %%
#|export
def test_missing_boxmeta_removes_the_base(box):
    bm, config, meta_path, base_path = box
    write_meta(meta_path, BOXMETA % '["work"]')
    record_meta_base(config, bm)
    assert base_path.is_file()

    # The box was excluded, or deleted, or never had a boxmeta here.
    meta_path.unlink()
    record_meta_base(config, bm)

    assert not base_path.exists()


def test_a_failed_write_removes_the_base_rather_than_leaving_the_old_one(box, monkeypatch):
    bm, config, meta_path, base_path = box
    write_meta(meta_path, BOXMETA % '["work"]')
    record_meta_base(config, bm)

    write_meta(meta_path, BOXMETA % '["work", "archived"]')

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("shutil.copyfile", boom)
    with pytest.raises(OSError):
        record_meta_base(config, bm)

    # The PREVIOUS base is gone. Keeping it would leave a base that no longer
    # matches any shared state, which is the one thing worse than no base.
    assert not base_path.exists()
    assert not list(base_path.parent.glob(".meta.base.*")), "a temp file was left behind"

# %% [markdown]
# ## Reading declines rather than guessing

# %%
#|export
def test_no_base_reads_as_none(box):
    bm, config, _, _ = box
    assert read_meta_base(config, bm) is None


def test_a_corrupt_base_is_removed_and_reads_as_none(box):
    bm, config, _, base_path = box
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text("this is not = valid toml [[[\n", encoding="utf-8")

    assert read_meta_base(config, bm) is None
    # Removed, not left: a merge against a half-read file would be worse than
    # no merge, and leaving it would make every later read pay the same cost.
    assert not base_path.exists()


def test_a_base_reads_back_with_the_box_identity(box):
    bm, config, meta_path, _ = box
    write_meta(meta_path, BOXMETA % '["work", "archived"]')
    record_meta_base(config, bm)

    base = read_meta_base(config, bm)
    assert base is not None
    assert base.groups == ["work", "archived"]
    # The identity fields are NOT in the file -- they come from the box the
    # base belongs to, exactly as `BoxMeta.load` derives them from a path.
    assert base.index_name == bm.index_name
    assert base.storage_location == bm.storage_location


def test_unknown_keys_survive_into_the_base(box):
    bm, config, meta_path, _ = box
    write_meta(meta_path, BOXMETA % "[]" + "written_by_a_newer_boxyard = 1\n")
    record_meta_base(config, bm)

    base = read_meta_base(config, bm)
    # A key from a newer boxyard has to round-trip, or a future merge would
    # compute that the other side ADDED it and this side never had it.
    assert base.unknown_keys == {"written_by_a_newer_boxyard": 1}

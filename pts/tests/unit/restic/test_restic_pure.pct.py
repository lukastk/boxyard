# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit tests for `_restic`'s pure parts
#
# Everything here runs without a restic binary and without a remote. The parts
# that need a real repo live in `test_restic_repo.py`.

# %%
#|default_exp unit.restic.test_restic_pure

# %%
#|export
import json
import os
from pathlib import Path

import pytest

from boxyard import const
from boxyard._restic import (
    PullMode,
    ResticCondition,
    anchored_include_args,
    apply_removals,
    data_root_exclude_args,
    parse_diff,
    parse_pointer,
    pointer_remote_path,
    read_state,
    repo_url_for_box,
    state_path,
    tree_modified_since,
    write_state,
)


# %% [markdown]
# ## Trap 1a: `backup --exclude` anchors on the ABSOLUTE path
#
# The behavioural test that a wrong spelling actually excludes the wrong set is
# in `test_restic_repo.py`, which drives real restic. This pins the SPELLING, so
# a change is caught even where restic is unavailable.

# %%
#|export
def test_exclude_arg_is_the_full_absolute_path():
    args = data_root_exclude_args(Path("/boxes/mybox/data"))
    assert args == ["--exclude", "/boxes/mybox/data/.boxyard-perms.json"]


def test_exclude_arg_is_not_a_bare_basename():
    """A bare basename would ALSO exclude a manifest nested inside the box."""
    _, pattern = data_root_exclude_args(Path("/boxes/mybox/data"))
    assert pattern != const.BOX_PERMS_MANIFEST_REL_PATH
    assert "/" in pattern


def test_exclude_arg_is_not_slash_anchored():
    """A leading slash anchors at the FILESYSTEM root and excludes nothing."""
    _, pattern = data_root_exclude_args(Path("/boxes/mybox/data"))
    assert pattern != "/" + const.BOX_PERMS_MANIFEST_REL_PATH


# %% [markdown]
# ## Trap 1b: `restore --include` anchors with a LEADING SLASH, relative to the subpath

# %%
#|export
def test_include_args_are_slash_anchored():
    assert anchored_include_args(["a/b.txt", "c.txt"]) == [
        "--include=/a/b.txt",
        "--include=/c.txt",
    ]


def test_include_args_do_not_double_the_slash():
    """Callers may pass either form; the result must be exactly one slash."""
    assert anchored_include_args(["/a/b.txt"]) == ["--include=/a/b.txt"]


def test_include_args_are_never_absolute():
    """An absolute pattern matches NOTHING under the subpath form."""
    args = anchored_include_args(["a/b.txt"])
    assert not any(a.startswith("--include=/tmp/") for a in args)


def test_include_args_empty_for_no_paths():
    assert anchored_include_args([]) == []


# %% [markdown]
# ## Paths

# %%
#|export
def test_repo_url_is_rclone_backed_and_beside_the_plain_parts():
    url = repo_url_for_box(Path("boxyard"), "hetzner-box", "20260101_abc__demo")
    assert url == "rclone:hetzner-box:boxyard/boxes/20260101_abc__demo/data.restic"


def test_repo_path_does_not_collide_with_plain_data():
    """A converted and an unconverted box must be distinguishable."""
    assert const.BOX_RESTIC_REL_PATH != const.BOX_DATA_REL_PATH


def test_pointer_sits_at_depth_2_beside_boxmeta():
    """
    This is what lets the bulk depth-2 listing already being run answer 'did
    this box's DATA move' for the whole yard at no extra remote calls.
    """
    pointer = pointer_remote_path(Path("boxyard"), "20260101_abc__demo")
    boxmeta = (
        Path("boxyard")
        / const.REMOTE_BOXES_REL_PATH
        / "20260101_abc__demo"
        / const.BOX_METAFILE_REL_PATH
    )
    assert pointer.parent == boxmeta.parent


# %% [markdown]
# ## `parse_diff`

# %%
#|export
SRC = "/home/u/dev/mybox"

DIFF_SAMPLE = f"""comparing snapshot aaaa to bbbb:

+    {SRC}/added.txt
-    {SRC}/gone.txt
M    {SRC}/sub/changed.txt
U    {SRC}/sub/owner.txt
T    {SRC}/sub/type.txt

Files:           1 new,     1 removed,     1 changed
"""


def test_parse_diff_splits_removed_from_everything_else():
    changed, removed = parse_diff(DIFF_SAMPLE, SRC)
    assert removed == ["gone.txt"]
    assert sorted(changed) == [
        "added.txt",
        "sub/changed.txt",
        "sub/owner.txt",
        "sub/type.txt",
    ]


def test_parse_diff_returns_paths_relative_to_the_source():
    changed, _ = parse_diff(DIFF_SAMPLE, SRC)
    assert not any(p.startswith("/") for p in changed)


def test_parse_diff_tolerates_a_trailing_slash_on_the_source():
    changed, removed = parse_diff(DIFF_SAMPLE, SRC + "/")
    assert removed == ["gone.txt"]
    assert "added.txt" in changed


def test_parse_diff_ignores_lines_outside_the_source():
    """A snapshot can hold more than one path; only ours is ours to act on."""
    other = DIFF_SAMPLE + "+    /somewhere/else/stray.txt\n"
    changed, _ = parse_diff(other, SRC)
    assert not any("stray" in p for p in changed)


def test_parse_diff_ignores_the_summary_lines():
    changed, removed = parse_diff(DIFF_SAMPLE, SRC)
    assert not any("Files:" in p for p in changed + removed)


def test_parse_diff_drops_the_root_itself():
    changed, _ = parse_diff(f"M    {SRC}\n", SRC)
    assert changed == []


# %% [markdown]
# ## `apply_removals`
#
# A path out of the repo is DATA, not an instruction.

# %%
#|export
def test_apply_removals_deletes_files_and_dirs(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("a")
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "deep.txt").write_text("d")

    removed = apply_removals(tmp_path, ["sub/a.txt", "tree"])

    assert removed == 2
    assert not (tmp_path / "sub" / "a.txt").exists()
    assert not (tmp_path / "tree").exists()
    assert (tmp_path / "keep.txt").exists()


def test_apply_removals_refuses_to_escape_the_data_dir(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")

    apply_removals(box, ["../outside.txt"])

    assert outside.exists(), "a `..` in a repo path escaped the box"


@pytest.mark.parametrize("rel", ["sub/..", ".", "sub/../..", "./"])
def test_apply_removals_refuses_a_path_that_normalises_into_the_box_root(
    tmp_path, rel
):
    """
    The escape the parent-directory check CANNOT catch, and the worst one.

    `box/sub/..` has a parent (`box/sub`) that is safely inside the box, so the
    parent check passes -- and the victim is the box itself, so `rmtree` takes
    the whole thing. Only normalising the relative path first stops it.
    """
    box = tmp_path / "box"
    (box / "sub").mkdir(parents=True)
    (box / "keep.txt").write_text("must survive")

    apply_removals(box, [rel])

    assert (box / "keep.txt").exists(), f"'{rel}' deleted the box itself"
    assert box.is_dir()


def test_apply_removals_refuses_an_absolute_path(tmp_path):
    box = tmp_path / "box"
    box.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")

    apply_removals(box, [str(outside)])

    assert outside.exists()


def test_apply_removals_ignores_paths_that_are_already_gone(tmp_path):
    assert apply_removals(tmp_path, ["never-existed.txt"]) == 0


def test_apply_removals_removes_a_dangling_symlink(tmp_path):
    os.symlink(tmp_path / "nowhere", tmp_path / "link")
    assert apply_removals(tmp_path, ["link"]) == 1
    assert not (tmp_path / "link").is_symlink()


def test_apply_removals_does_not_follow_a_symlink_out(tmp_path):
    """Deleting the link must not delete what it points at."""
    box = tmp_path / "box"
    box.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("must survive")
    os.symlink(target, box / "link.txt")

    apply_removals(box, ["link.txt"])

    assert target.exists()
    assert not (box / "link.txt").is_symlink()


# %% [markdown]
# ## The pointer
#
# A corrupt pointer must never make a box look UP TO DATE.

# %%
#|export
def test_parse_pointer_reads_a_good_one():
    pointer = parse_pointer(json.dumps({"snapshot": "abc", "path": "/box"}))
    assert pointer["snapshot"] == "abc"
    assert pointer["path"] == "/box"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "null",
        "[]",
        '"abc"',
        "{}",
        '{"path": "/box"}',
        '{"snapshot": ""}',
        '{"snapshot": null}',
    ],
)
def test_parse_pointer_returns_none_for_anything_unusable(raw):
    assert parse_pointer(raw) is None


# %% [markdown]
# ## The machine-local state record
#
# Degrades in ONE direction: unusable means "I do not know", never "up to date".

# %%
#|export
def test_state_round_trips(tmp_path):
    write_state(tmp_path, "20260101_abc__demo", "snap1", now_unix=1000.0)
    record = read_state(tmp_path, "20260101_abc__demo")
    assert record["snapshot"] == "snap1"
    assert record["synced_at_unix"] == 1000.0


def test_state_records_a_timestamp_even_when_not_given(tmp_path):
    """
    Without this, a box whose base is later forgotten has no repo-independent
    way to tell whether it holds local changes -- which is what turned a clean
    replica into a permanent false CONFLICT.
    """
    write_state(tmp_path, "b", "snap1")
    assert read_state(tmp_path, "b")["synced_at_unix"] > 0


def test_state_is_absent_before_anything_is_written(tmp_path):
    assert read_state(tmp_path, "never-synced") is None


@pytest.mark.parametrize("body", ["", "{", "null", "[]", '{"synced_at_unix": 1}'])
def test_unusable_state_reads_as_unknown(tmp_path, body):
    path = state_path(tmp_path, "b")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    assert read_state(tmp_path, "b") is None


def test_state_write_is_atomic_leaving_no_temp_files(tmp_path):
    write_state(tmp_path, "b", "snap1")
    write_state(tmp_path, "b", "snap2")
    leftovers = [p for p in state_path(tmp_path, "b").parent.iterdir()
                 if p.name.startswith(".tmp-")]
    assert leftovers == []
    assert read_state(tmp_path, "b")["snapshot"] == "snap2"


def test_state_is_not_inside_the_synced_parts(tmp_path):
    """
    It is a fact about THIS machine, like a placement record. Putting it in
    boxmeta or conf/ would sync it and make every machine claim every other
    machine's position.
    """
    assert const.RESTIC_STATE_REL_PATH in state_path(tmp_path, "b").parts


# %% [markdown]
# ## The repo-independent fallback

# %%
#|export
def test_tree_modified_since_sees_a_new_file(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    os.utime(tmp_path / "a.txt", (5000, 5000))
    assert tree_modified_since(tmp_path, 1000.0) is True


def test_tree_modified_since_is_false_for_an_older_tree(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    os.utime(tmp_path / "a.txt", (1000, 1000))
    os.utime(tmp_path, (1000, 1000))
    assert tree_modified_since(tmp_path, 5000.0) is False


def test_tree_modified_since_is_false_for_an_empty_tree(tmp_path):
    assert tree_modified_since(tmp_path, 1000.0) is False


# %% [markdown]
# ## Enum shapes
#
# These names are load-bearing: they map onto `SyncCondition` mechanically, and
# each `PullMode` records WHY a full restore was taken so diagnostics stay
# honest about the difference between "forgotten" and "path mismatch".

# %%
#|export
def test_conditions_line_up_with_sync_conditions():
    from boxyard._models import SyncCondition

    for condition in ResticCondition:
        if condition is ResticCondition.UNINITIALISED:
            continue
        assert condition.value in {c.value for c in SyncCondition}


def test_every_full_restore_reason_is_distinguishable():
    values = [m.value for m in PullMode]
    assert len(values) == len(set(values))
    assert {
        "full",
        "full-no-base",
        "full-base-forgotten",
        "full-path-mismatch",
        "full-diff-failed",
        "diff",
    } == set(values)

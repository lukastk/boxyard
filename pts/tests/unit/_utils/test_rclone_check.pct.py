# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for `rclone_check` and the write-ownership probe
#
# The probe answers "would pushing this actually move anything?", and the whole
# write-ownership feature rests on it: without it a read-only machine reports
# local changes that do not exist, forever.
#
# Two properties are easy to get wrong and impossible to notice afterwards:
#
# 1. **Identical content with a different mtime is NOT a change.** This is why
#    the probe uses `rclone check --combined` rather than parsing
#    `sync --dry-run`: measured, the dry run prints
#    `Skipped update modification time as --dry-run is set` for exactly this
#    case, so any "did the dry run mention the file?" test reports a false
#    change — reintroducing the bug the probe exists to remove.
# 2. **"Could not look" must never be reported as "no differences".** rclone
#    exits non-zero both for "found differences" and for "unreachable remote",
#    so the two are separated by whether any output was produced at all.

# %%
#|default_exp unit._utils.test_rclone_check

# %%
#|export
import asyncio
import os

import pytest

from boxyard._utils import rclone_check


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def pair(tmp_path):
    """A local source dir and an rclone alias remote pointing at a dest dir."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    conf = tmp_path / "rclone.conf"
    conf.write_text(f"[dst]\ntype = alias\nremote = {dst}\n")
    return conf, src, dst


def check(conf, src, **kwargs):
    return run(
        rclone_check(
            rclone_config_path=conf,
            source="",
            source_path=str(src),
            dest="dst",
            dest_path="",
            **kwargs,
        )
    )


# %%
#|export
class TestRcloneCheck:
    def test_both_sides_empty_is_answerable_and_clean(self, pair):
        conf, src, _ = pair
        assert check(conf, src) == (True, [])

    def test_identical_content_is_clean(self, pair):
        conf, src, dst = pair
        (src / "f.txt").write_text("a")
        (dst / "f.txt").write_text("a")
        assert check(conf, src) == (True, [])

    def test_identical_content_with_a_different_mtime_is_clean(self, pair):
        """
        The case that rules out parsing `sync --dry-run`. If this ever starts
        reporting a difference, every read-only machine gains a permanent,
        false "you have local changes".
        """
        conf, src, dst = pair
        (src / "f.txt").write_text("a")
        (dst / "f.txt").write_text("a")
        os.utime(dst / "f.txt", (0, 0))
        assert check(conf, src) == (True, [])

    def test_a_file_only_on_the_source_is_a_difference(self, pair):
        conf, src, _ = pair
        (src / "new.txt").write_text("n")
        assert check(conf, src) == (True, ["new.txt"])

    def test_a_file_only_on_the_destination_is_a_difference(self, pair):
        """A sync would DELETE it, which is a change to the remote."""
        conf, src, dst = pair
        (dst / "extra.txt").write_text("e")
        assert check(conf, src) == (True, ["extra.txt"])

    def test_differing_content_is_a_difference(self, pair):
        conf, src, dst = pair
        (src / "f.txt").write_text("a")
        (dst / "f.txt").write_text("b")
        assert check(conf, src) == (True, ["f.txt"])

    def test_an_exclude_file_is_honoured(self, pair):
        conf, src, dst = pair
        (src / "keep.txt").write_text("k")
        (dst / "keep.txt").write_text("k")
        (src / "scratch.tmp").write_text("junk")
        exclude = src.parent / "exclude"
        exclude.write_text("*.tmp\n")
        assert check(conf, src, exclude_file=str(exclude)) == (True, [])
        # ...and without it, the same file IS a difference.
        assert check(conf, src) == (True, ["scratch.tmp"])

    def test_an_unreachable_remote_is_not_answerable(self, pair):
        """
        Must not come back as "no differences". rclone exits 1 both for
        "found differences" and for "could not look", so a caller that trusted
        the exit code alone would call an unreachable box clean.
        """
        conf, src, _ = pair
        (src / "f.txt").write_text("a")
        answered, differing = run(
            rclone_check(
                rclone_config_path=conf,
                source="",
                source_path=str(src),
                dest="no_such_remote",
                dest_path="",
            )
        )
        assert answered is False
        assert differing == []


# %%
#|export
class TestPushWouldTransfer:
    """
    The probe wrapping `rclone_check`. Its one non-obvious rule: an
    unanswerable check counts as "would transfer", so a box is reported rather
    than silently declared clean because the remote could not be reached.
    """

    def _config(self, conf_path, tmp_path):
        from boxyard.config import (
            BoxTimestampFormat,
            Config,
            StorageConfig,
            StorageType,
        )

        return Config(
            config_path=conf_path.parent / "config.toml",
            default_storage_location="dst",
            boxyard_data_path=tmp_path / "data",
            box_timestamp_format=BoxTimestampFormat.DATE_ONLY,
            user_boxes_path=tmp_path / "boxes",
            user_box_groups_path=tmp_path / "groups",
            storage_locations={
                "dst": StorageConfig(
                    storage_type=StorageType.RCLONE, store_path=tmp_path / "store"
                )
            },
            box_groups={},
            virtual_box_groups={},
            default_box_groups=[],
            box_subid_character_set="abcdefghijklmnopqrstuvwxyz0123456789",
            box_subid_length=6,
            max_concurrent_rclone_ops=2,
        )

    def _probe(self, tmp_path, pair, dest="dst"):
        from pathlib import Path

        from boxyard._ownership import push_would_transfer

        conf, src, _ = pair
        config = self._config(conf, tmp_path)
        return run(
            push_would_transfer(
                _ConfigWithRcloneConf(config, conf),
                local_path=Path(src),
                remote=dest,
                remote_path=Path(""),
            )
        )

    def test_a_clean_box_would_transfer_nothing(self, tmp_path, pair):
        conf, src, dst = pair
        (src / "f.txt").write_text("a")
        (dst / "f.txt").write_text("a")
        assert self._probe(tmp_path, pair) is False

    def test_a_real_change_would_transfer(self, tmp_path, pair):
        conf, src, _ = pair
        (src / "work.txt").write_text("real work")
        assert self._probe(tmp_path, pair) is True

    def test_an_unanswerable_probe_counts_as_would_transfer(self, tmp_path, pair):
        """
        The safe direction. Reporting a box we could not check is a state the
        user can see and act on; declaring it clean would hide real work.
        """
        conf, src, _ = pair
        (src / "work.txt").write_text("real work")
        assert self._probe(tmp_path, pair, dest="no_such_remote") is True


# %%
#|export
class _ConfigWithRcloneConf:
    """
    A `Config` with `rclone_config_path` pointed at the fixture's file.

    `Config.rclone_config_path` is a property derived from `config_path`, so it
    cannot be assigned; this forwards everything else untouched.
    """

    def __init__(self, config, rclone_config_path):
        self._config = config
        self.rclone_config_path = rclone_config_path

    def __getattr__(self, name):
        return getattr(self._config, name)

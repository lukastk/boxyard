# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Every test defined in `pts/` must exist in the exported module
#
# A `# %%` cell without `#|export` is dropped from the exported module
# **silently**: `nbl export` says nothing, pytest happily collects a file that
# looks complete, and the suite goes green while the test never runs.
#
# That is not hypothetical. A regression test written for a real defect — a
# tree comparison that ignored the box's excludes — did not run at all for
# exactly this reason, and was found only by mutating the fix away and noticing
# that `-k compare_trees` selected 3 tests where it should have selected 5.
#
# The failure mode is the worst kind: it does not fail, it does not warn, and
# the artefact it produces looks right. So this compares the two directly.

# %%
#|default_exp unit.test_export_completeness

# %%
#|export
import re
from pathlib import Path

import pytest


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "pts").is_dir():
            return parent
    pytest.skip("not running from a source checkout")


def exported_path(root: Path, pct: Path) -> Path:
    """`pts/tests/a/b.pct.py` -> `src/tests/a/b.py`."""
    rel = pct.relative_to(root / "pts" / "tests")
    return root / "src" / "tests" / rel.with_suffix("").with_suffix(".py")


def missing_from_export(pct_text: str, exported_text: str) -> "set[str]":
    """
    Test functions defined in the pct source but absent from the export.

    Both sides match INDENTED defs too: half the suite's tests are methods on
    Test* classes, and a top-level-only pattern on the source side made every
    one of them invisible to this guard -- a dropped class cell would have
    sailed through. One comparison function, used by the guard AND its
    self-check, so the self-check exercises the real comparison rather than a
    re-implementation that can drift.
    """
    defined = set(re.findall(r"^\s*def (test_\w+)", pct_text, re.M))
    present = set(re.findall(r"^\s*def (test_\w+)", exported_text, re.M))
    return defined - present


# %%
#|export
def test_every_test_function_reaches_the_exported_module():
    """
    The guard. Any test function defined in `pts/` and missing from `src/` is a
    test that cannot possibly have run, however green the suite looked.
    """
    root = repo_root()
    sources = sorted((root / "pts" / "tests").glob("**/*.pct.py"))
    assert sources, "no test sources found -- this guard would pass vacuously"

    missing: list[str] = []
    compared = 0
    for pct in sources:
        exported = exported_path(root, pct)
        pct_text = pct.read_text()
        if not re.search(r"^\s*def test_\w+", pct_text, re.M):
            continue
        if not exported.exists():
            missing.append(f"{pct.name}: no exported module at all")
            continue
        dropped = missing_from_export(pct_text, exported.read_text())
        compared += len(re.findall(r"^\s*def (test_\w+)", pct_text, re.M))
        for name in sorted(dropped):
            missing.append(f"{pct.relative_to(root)}: {name}")

    assert compared > 0, "compared nothing -- the guard would pass vacuously"
    assert not missing, (
        "these tests are defined but were never exported, so they do NOT run "
        "(a `# %%` cell without `#|export` is dropped silently):\n  "
        + "\n  ".join(missing)
        + "\nAdd `#|export` to the cell and re-run `nbl export`."
    )


def test_the_guard_would_notice_a_dropped_test():
    """
    The guard checked against itself, THROUGH the real comparison function --
    a self-check that re-implements the comparison can drift from it and then
    proves nothing. Covers both drop shapes: a top-level function and a
    class-based method (the shape the guard was blind to at first).
    """
    pct_text = (
        "# %%\n#|export\ndef test_kept():\n    pass\n\n"
        "# %%\ndef test_dropped():\n    pass\n\n"
        "# %%\nclass TestDroppedClass:\n"
        "    def test_dropped_method(self):\n        pass\n"
    )
    exported_text = "def test_kept():\n    pass\n"
    assert missing_from_export(pct_text, exported_text) == {
        "test_dropped",
        "test_dropped_method",
    }

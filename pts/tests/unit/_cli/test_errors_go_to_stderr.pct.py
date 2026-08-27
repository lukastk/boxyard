# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # A Failing Command Writes Nothing To stdout
#
# Many CLI error paths used `typer.echo("...")` with no `err=True`, so the
# message landed on **stdout** while the command exited 1. A caller that pipes
# stdout and reasonably discards stderr then reads the error text as data.
#
# That is not hypothetical. A cockpit command read a box's groups with
#
#     boxyard list-groups --box "$index" 2>/dev/null | while read -r g; do ...
#
# and forwarded each line to `boxyard new -g`. With the box absent, the error
# line `Box with index name ... not found.` became a `-g` argument, and the
# failure surfaced two layers later as a pydantic ValidationError quoting the
# error message back as an invalid group name.
#
# Two tests here, and the second is the one that matters:
#
# * a behavioural check on the case that actually bit, and
# * a LINT over the source, because the fix was a 28-site sweep and the thing
#   worth protecting is the rule, not the sites. A new error path added next
#   month is exactly what would drift.

# %%
#|default_exp unit._cli.test_errors_go_to_stderr

# %%
#|export
import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import boxyard._cli.main as _main_module
import boxyard._cli.multi_sync as _multi_sync_module
from boxyard._cli.app import app

# %% [markdown]
# ## The case that bit

# %%
#|export
def test_a_missing_box_says_nothing_on_stdout(tmp_path, monkeypatch):
    from tests.integration.conftest import create_boxyards

    _, _, _, config_path, _ = create_boxyards()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config", str(config_path), "list-groups", "--box", "no-such-box"],
        catch_exceptions=False,
    )

    assert result.exit_code == 1
    # `list-groups` prints one group per line, so an error on stdout is
    # indistinguishable from a group named after it.
    assert result.stdout == "", f"error text reached stdout: {result.stdout!r}"

# %% [markdown]
# ## The rule, over the source
#
# Every `typer.echo` immediately followed by a non-zero exit must pass
# `err=True`. Mechanical enough to sweep, and mechanical enough to check.

# %%
#|export
_EXIT = re.compile(r"raise typer\.Exit\((code=)?[1-9]|(sys\.)?exit\([1-9]")


def _echo_calls_followed_by_a_failing_exit(source: str):
    """Yield (line number, source) for each `typer.echo` before a non-zero exit."""
    lines = source.split("\n")
    for i, line in enumerate(lines):
        if not re.match(r"^\s*typer\.echo\(", line):
            continue
        # The call may span lines; follow the parentheses.
        j, depth = i, line.count("(") - line.count(")")
        while depth > 0 and j + 1 < len(lines):
            j += 1
            depth += lines[j].count("(") - lines[j].count(")")
        call = "\n".join(lines[i : j + 1])
        k = j + 1
        while k < len(lines) and (not lines[k].strip() or lines[k].strip().startswith("#")):
            k += 1
        if k < len(lines) and _EXIT.match(lines[k].strip()):
            yield i + 1, call


@pytest.mark.parametrize(
    "module", [_main_module, _multi_sync_module], ids=lambda m: m.__name__
)
def test_every_error_echo_goes_to_stderr(module):
    source = Path(module.__file__).read_text()
    offenders = [
        (n, c.strip()[:70])
        for n, c in _echo_calls_followed_by_a_failing_exit(source)
        if "err=True" not in c
    ]
    assert not offenders, (
        f"{module.__name__}: these print to stdout and then exit non-zero, so a "
        f"caller piping stdout reads them as data:\n"
        + "\n".join(f"  line {n}: {c}" for n, c in offenders)
    )


def test_the_lint_finds_something():
    """A regex that silently matched nothing would make the check vacuous."""
    source = Path(_main_module.__file__).read_text()
    found = list(_echo_calls_followed_by_a_failing_exit(source))
    assert len(found) >= 20, f"only found {len(found)} error-then-exit sites"
    assert all("err=True" in c for _, c in found)

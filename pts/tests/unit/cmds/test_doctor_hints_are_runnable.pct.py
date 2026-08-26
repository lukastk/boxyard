# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Every Command `doctor` Suggests Must Actually Parse
#
# Doctor's own docstring says every hint "names an exact command that is safe to
# run verbatim". That was a rule nothing enforced, and one hint had been wrong
# since the check was written:
#
# ```
# boxyard sync -r '<box>' --sync-direction to_remote --sync-setting force
# ```
#
# `SyncDirection` is `push`/`pull`. `to_remote` is not a member, so the command
# exits 2 with *"'to_remote' is not one of 'push', 'pull'"* — and this is the
# hint for `diverged-box`, the check added in v0.4.7 specifically so a wedged
# box would be visible. Two boxes on this fleet had been wedged since June, with
# doctor telling anyone who looked to run a command that could not run.
#
# The test reads the hints out of doctor's SOURCE rather than out of a report:
# most checks need a broken yard to fire, and `diverged-box` needs a broken
# REMOTE, so a report-driven test would not have covered the one that was wrong.
#
# Commands are PARSED, never invoked — `make_context` validates options and
# values with no side effects, which matters because some of the suggestions
# (`delete`, `init`, `create-user-symlinks`) do real work.

# %%
#|default_exp unit.cmds.test_doctor_hints_are_runnable

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import ast
import inspect
import re
import shlex

import click
import pytest
import typer

import boxyard.cmds._doctor as _doctor_module
from boxyard._cli.app import app


# A hint is a template: `{box_index_name}` and the like are filled in at
# runtime. Substituting a plausible value keeps shlex and click honest without
# pretending the placeholder is real.
_PLACEHOLDER = "PLACEHOLDER"

_COMMAND_RE = re.compile(r"`(boxyard [^`]*)`")


def _literal_strings(source: str) -> list[str]:
    """
    Every string literal in the module, with f-string holes filled in.

    Adjacent literals are concatenated by the PARSER, so a hint split across
    several `f"..."` fragments arrives here as one string — which is exactly the
    shape the broken hint had.
    """
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            text = ""
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    text += part.value
                else:
                    text += _PLACEHOLDER
            out.append(text)
    return out


def _suggested_commands() -> list[str]:
    source = inspect.getsource(_doctor_module)
    commands = set()
    for literal in _literal_strings(source):
        for match in _COMMAND_RE.finditer(literal):
            commands.add(match.group(1).strip())
    return sorted(commands)

# %% [markdown]
# ## The extraction finds something to check
#
# A regex that silently matched nothing would make the whole test vacuous.

# %%
#|export
def test_hints_contain_commands():
    commands = _suggested_commands()
    assert len(commands) >= 10, commands
    # TESTREF: test_doctor_hints_are_runnable
    assert any("--sync-direction" in c for c in commands), (
        "the diverged-box hint was not extracted, so the case this test exists "
        "for is not covered"
    )

# %% [markdown]
# ## Every one of them parses

# %%
#|export
@pytest.mark.parametrize("command", _suggested_commands())
def test_suggested_command_parses(command):
    cli = typer.main.get_command(app)
    argv = shlex.split(command)[1:]  # drop the leading "boxyard"
    assert argv, command

    name, *args = argv
    sub = cli.commands.get(name)
    assert sub is not None, f"`{command}` names a subcommand that does not exist"

    try:
        # PARSE only. Several of these commands do real work; make_context
        # validates flags and values without invoking anything.
        sub.make_context(name, list(args), resilient_parsing=False)
    except click.MissingParameter:
        # A bare mention like `boxyard copy` is a template, not a full command.
        # Missing a required option is fine; a WRONG one is not.
        pass
    except click.exceptions.Exit:
        pass
    except click.UsageError as e:
        # A hint may interpolate a value into a CHOICE option
        # (`--sync-choices {part}`). The value is the operator's, not the
        # hint's, so a placeholder failing the choice check says nothing --
        # but a LITERAL failing it is exactly the bug this test exists for
        # (`--sync-direction to_remote`), and that message names the literal.
        if f"'{_PLACEHOLDER}'" in str(e):
            return
        pytest.fail(f"`{command}` does not parse: {e}")

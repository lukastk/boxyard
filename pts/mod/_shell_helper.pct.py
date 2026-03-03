# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _shell_helper
#
# Lightweight shell integration helper for fuzzy box path lookup.
# Uses `BoxyardFast` for fast startup (~27ms). Invoked as:
#
#     python3 -m boxyard._shell_helper search <term>
#
# Outputs tab-separated `display\trelpath` lines for shell widget consumption.

# %%
#|default_exp _shell_helper

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
import sys
import os
import argparse
from pathlib import Path

from boxyard._fast import BoxyardFast

_DEFAULT_CONFIG_PATH = Path("~/.config/boxyard/config.toml")


def search(term: str | None = None, config_path: Path | None = None):
    """Search boxes by name (case-insensitive contains) and print matches.

    If term is None or empty, all boxes are listed.

    Output format: one line per match, tab-separated:
        display_string\\trelative_path
    """
    if config_path is None:
        env = os.environ.get("BOXYARD_CONFIG_PATH")
        config_path = Path(env) if env else _DEFAULT_CONFIG_PATH

    bf = BoxyardFast.from_file(config_path=config_path)
    user_boxes_path = Path(bf._user_boxes_path or "~/boxes").expanduser()
    cwd = Path.cwd()
    term_lower = term.lower() if term else None

    matches = []
    for bm in bf._boxes:
        if term_lower and term_lower not in bm["name"].lower():
            continue

        index_name = bm["_index_name"]
        box_id = bm["_box_id"]
        box_path = user_boxes_path / index_name
        included = box_path.exists() or box_path.is_symlink()
        rel = os.path.relpath(box_path, cwd)

        marker = "\u25cf" if included else "\u25cb"
        groups = bm.get("groups", [])
        group_tag = f" [{', '.join(groups)}]" if groups else ""
        display = f"{marker} {bm['name']} ({box_id}){group_tag}"

        matches.append((display, rel))

    matches.sort(key=lambda m: m[0])
    for display, rel in matches:
        print(f"{display}\t{rel}")


def main():
    parser = argparse.ArgumentParser(prog="boxyard._shell_helper")
    sub = parser.add_subparsers(dest="command")

    search_p = sub.add_parser("search", help="Fuzzy search boxes by name")
    search_p.add_argument("term", nargs="?", default=None, help="Search term (case-insensitive substring). Lists all boxes if omitted.")

    args = parser.parse_args()
    if args.command == "search":
        search(args.term)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

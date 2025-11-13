# %% [markdown]
# # _utils.base

# %%
#|default_exp _utils.base

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import repoyard._utils as this_module

# %%
#|export
import subprocess
import shlex
import json
from enum import Enum
from repoyard import const
import repoyard.config
from pathlib import Path

# %%
#|hide
show_doc(this_module.get_repo_full_name_from_sub_path)


# %%
#|export
def get_repo_full_name_from_sub_path(
    config: repoyard.config.Config,
    sub_path: str,
) -> Path|None:
    """
    Get the full name of a synced repo from a path inside of the repo.
    """
    sub_path = Path(sub_path).expanduser()
    is_in_local_store_path = sub_path.is_relative_to(config.local_store_path)
    
    if not is_in_local_store_path:
        return None
    
    rel_path = sub_path.relative_to(config.local_store_path)
    
    if len(rel_path.parts) < 2: # The path is not inside a repo
        return None
    
    repo_full_name = rel_path.parts[2]
    return repo_full_name


# %%
#|hide
show_doc(this_module.get_repo_full_name_from_cwd)


# %%
#|export
def get_repo_full_name_from_cwd(
    config: repoyard.config.Config,
) -> Path|None:
    """
    Get the full name of a synced repo from a path inside of the repo.
    """
    import os
    cwd = os.getcwd()
    return get_repo_full_name_from_sub_path(config, cwd)


# %%
#|hide
show_doc(this_module.get_hostname)

# %%
#|export
import platform
import subprocess

def get_hostname():
    system = platform.system()
    hostname = None
    if system == "Darwin":
        # Mac
        try:
            result = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, check=True)
            hostname = result.stdout.strip()
        except Exception:
            hostname = None
    if hostname is None:
        hostname = platform.node()
    return hostname


# %%
#|hide
show_doc(this_module.run_fzf)


# %%
#|export
def run_fzf(terms: list[str], disp_terms: list[str]|None=None):
    """
    Launches the fzf command-line fuzzy finder with a list of terms and returns
    the selected term.

    Parameters:
    terms (List[str]): A list of strings to be presented to fzf for selection.

    Returns:
    str or None: The selected string from fzf, or None if no selection was made
    or if fzf encountered an error.

    Raises:
    RuntimeError: If fzf is not installed or not found in the system PATH.
    """
    import subprocess
    if disp_terms is None: disp_terms = terms
    try:
        # Launch fzf with the list of strings
        result = subprocess.run(
            ['fzf'],
            input='\n'.join(disp_terms),
            text=True,
            capture_output=True
        )
        res_term = result.stdout.strip()
        term_index = [t.strip() for t in disp_terms].index(res_term)
        sel_term = terms[term_index]
        # Return the selected string or None if no selection was made
        if result.returncode != 0: 
            return None, None
        else: 
            return term_index, sel_term
    except FileNotFoundError:
        raise RuntimeError("fzf is not installed or not found in PATH.")

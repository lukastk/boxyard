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
show_doc(this_module.get_synced_repo_full_name_from_sub_path)


# %%
#|export
def get_synced_repo_full_name_from_sub_path(
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

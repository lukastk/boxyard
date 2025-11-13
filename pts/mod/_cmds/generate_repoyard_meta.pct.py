# %% [markdown]
# # sync

# %%
#|default_exp _cmds.sync_repometas
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import repoyard._cmds.sync as this_module

# %%
#|top_export
import typer
from typer import Argument, Option
from typing_extensions import Annotated
from pathlib import Path
import os

from repoyard._cli import app
from repoyard import const


# %%
#|set_func_signature
@app.command(name='sync')
def sync(
    config_path: Path|None = None,
    storage_location: str|None = Option(None, "--location", "-l", help="The storage location to use for the new repository."),
    repo: str|None = Option(None, "--repo", "-r", help="The full name of the repository, the id or the path of the repo."),
    repo_id: str|None = Option(None, "--repo-id", "-i", help="The id of the repository to sync."),
):
    """
    """
    ...


# %% [markdown]
# **Notes**:
#     
# - Check if the repo exists on the location. If not, then do a one-way sync.
# - Do a dry-run first always and report any errors, especially conflicts.

# %%
# Set up test environment
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = tests_working_dir / "sync_test"
data_path = test_folder_path / ".repoyard"
# !rm -rf {test_folder_path}

# %%
from repoyard._repos import RepoMeta
repo_meta = RepoMeta(
    name = "test_repo",
    storage_location = "local",
    creator_hostname = "me",
    groups = [],
)
repo_meta.to_toml()



# %%
config.local_repometa_path

# %%
config.local_repostore_path

# %% [markdown]
# Set up testing args

# %%
config_path = test_folder_path / ".config" / "repoyard" / "config.toml"
storage_location = None





# %%
# Run init
import subprocess
subprocess.run(
    ["repoyard", "init", "--config-path", config_path, "--data-path", data_path],
    stdout=subprocess.DEVNULL,
);

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
from repoyard.config import get_config
if config_path is None:
    config_path = const.DEFAULT_CONFIG_PATH
config = get_config(config_path)
    
if storage_location is None:
    storage_location = config.default_storage_location
    
if repo_full_name is not None and repo_id is not None:
    raise ValueError("Cannot provide both repo_full_name and repo_id")
if repo_full_name is None and repo_id is None:
    from repoyard._utils import get_synced_repo_full_name_from_sub_path
    get_synced_repo_full_name_from_sub_path(config, os.getcwd())

# %%
# Create a test repos
# !mkdir -p {data_path / "test_repo"}

# %% [markdown]
# Find the repo meta

# %%
#|export
from repoyard._repos import load_repo_meta

load_repo_meta(config, Path("my_repo_meta.toml"))

# %%
#|export
from repoyard._utils import rclone_path_exists

rclone_path_exists(
    config.rclone_config_path,
    "my_remote",
    "file1.txt",
)

# %%
#|export
from repoyard._repos import get_all_repo_metas

repo_metas = get_all_repo_metas(config)

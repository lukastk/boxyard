# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # _new_repo

# %%
#|default_exp cmds._new_repo
#|export_as_func true

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|top_export
from pathlib import Path
import subprocess
import re
from datetime import datetime


def _extract_repo_name_from_git_url(url: str) -> str:
    """Extract repository name from a git URL (SSH or HTTPS)."""
    # Remove trailing .git if present
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]

    # Handle SSH format: git@host:user/repo
    ssh_match = re.match(r"^git@[^:]+:(.+)$", url)
    if ssh_match:
        path = ssh_match.group(1)
        return path.split("/")[-1]

    # Handle HTTPS/HTTP format: https://host/user/repo
    https_match = re.match(r"^https?://[^/]+/(.+)$", url)
    if https_match:
        path = https_match.group(1)
        return path.split("/")[-1]

    # Fallback: just take the last path component
    return url.split("/")[-1]

# %%
#|set_func_signature
def new_repo(
    config_path: Path,
    storage_location: str | None = None,
    repo_name: str | None = None,
    from_path: Path | None = None,
    copy_from_path: bool = False,
    creator_hostname: str | None = None,
    creation_timestamp_utc: datetime | None = None,
    initialise_git: bool = True,
    verbose: bool = False,
    git_clone_url: str | None = None,
):
    """
    Create a new repoyard repository.

    Args:
        config_path: The path to the repoyard config file.
        storage_location: The storage location to create the new repository in.
        repo_name: The name of the new repository.
        from_path: The path to a local directory to move into repoyard as a new repository.
        copy_from_path: Whether to copy the contents of the from_path into the new repository.
        creator_hostname: The hostname of the creator of the new repository.
        creation_timestamp_utc: The timestamp of the new repository. If not provided, the current UTC timestamp will be used.
        initialise_git: Whether to initialise a git repository in the new repository.
        verbose: Whether to print verbose output.
        git_clone_url: Git URL (SSH or HTTPS) to clone as the new repository.

    Returns:
        The index name of the new repository.
    """
    ...

# %% [markdown]
# Set up testing args

# %%
from tests.utils import *

remote_name, remote_rclone_path, config, config_path, data_path = create_repoyards()

# %%
# Args
config_path = config_path
storage_location = None
repo_name = "test_repo"
from_path = None
copy_from_path = False
creator_hostname = None
add_rclone_exclude = True
creation_timestamp_utc = None
initialise_git = True
verbose = True
git_clone_url = None

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
from repoyard.config import get_config

config = get_config(config_path)

if storage_location is None:
    storage_location = config.default_storage_location

if storage_location not in config.storage_locations:
    raise ValueError(
        f"Invalid storage location: {storage_location}. Must be one of: {', '.join(config.storage_locations)}."
    )

if git_clone_url is not None and from_path is not None:
    raise ValueError("`git_clone_url` and `from_path` are mutually exclusive.")

if repo_name is None and from_path is None and git_clone_url is None:
    raise ValueError("Either `repo_name`, `from_path`, or `git_clone_url` must be provided.")

if from_path is not None:
    from_path = Path(from_path).expanduser().resolve()

if from_path is not None and repo_name is None:
    repo_name = from_path.name

if git_clone_url is not None and repo_name is None:
    repo_name = _extract_repo_name_from_git_url(git_clone_url)

if from_path is None and copy_from_path:
    raise ValueError("`from_path` must be provided if `copy_from_path` is True.")

from repoyard._utils import get_hostname

if creator_hostname is None:
    creator_hostname = get_hostname()

# %% [markdown]
# Check if the `from_path` is a repo within the repoyard

# %%
#|export
from repoyard._models import get_repoyard_meta, RepoPart

repoyard_meta = get_repoyard_meta(config)

if from_path is not None:
    from_path = Path(from_path).expanduser().resolve()
    repo_paths = [
        repo_meta.get_local_part_path(config, RepoPart.DATA)
        for repo_meta in repoyard_meta.repo_metas
    ]

    if from_path in repo_paths and not copy_from_path:
        raise ValueError(
            f"'{from_path}' is already a repoyard repository. Use `copy_from_path=True` to copy the contents of this repo into a new repo."
        )

# %% [markdown]
# Create meta file

# %%
#|export
from repoyard._models import RepoMeta

repo_meta = RepoMeta.create(
    config,
    name=repo_name,
    storage_location_name=storage_location,
    groups=config.default_repo_groups,
    creator_hostname=creator_hostname,
    creation_timestamp_utc=creation_timestamp_utc,
)

repo_meta.save(config)

# %% [markdown]
# Create the repo folder

# %%
#|export
from repoyard._models import RepoPart

repo_path = repo_meta.get_local_path(config)
repo_data_path = repo_meta.get_local_part_path(config, RepoPart.DATA)
repo_conf_path = repo_meta.get_local_part_path(config, RepoPart.CONF)
repo_path.mkdir(parents=True, exist_ok=True)
repo_conf_path.mkdir(parents=True, exist_ok=True)

if git_clone_url is not None:
    if verbose:
        print(f"Cloning {git_clone_url}")
    res = subprocess.run(
        ["git", "clone", git_clone_url, str(repo_data_path)],
        check=True,
        stdout=subprocess.DEVNULL if not verbose else None,
        stderr=subprocess.DEVNULL if not verbose else None,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to clone repository from {git_clone_url}")
elif from_path is not None:
    if copy_from_path:
        import shutil

        shutil.copytree(
            from_path, repo_data_path
        )  # TESTREF: test_new_repo_copy_from_path
    else:
        from_path.rename(repo_data_path)
else:
    repo_data_path.mkdir(parents=True, exist_ok=True)

# %% [markdown]
# Run `git init`

# %%
#|export
# Skip git init when cloning (already a git repo) or when .git exists
if initialise_git and git_clone_url is None and not (repo_data_path / ".git").exists():
    if verbose:
        print("Initialising git repository")
    res = subprocess.run(
        ["git", "init"],
        check=True,
        cwd=repo_data_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if res.returncode != 0:
        if verbose:
            print("Warning: Failed to initialise git repository")

# %% [markdown]
# Refresh the repoyard meta file

# %%
#|export
from repoyard._models import refresh_repoyard_meta

refresh_repoyard_meta(config)

# %% [markdown]
# Return repo index name

# %%
#|func_return
repo_meta.index_name

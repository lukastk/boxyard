# %% [markdown]
# # new

# %%
#|default_exp _cmds.new
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import repoyard._cmds.new as this_module

# %%
#|top_export
from pathlib import Path
import subprocess

from repoyard import const


# %%
#|set_func_signature
def new(
    config_path: Path|None = None,
    storage_location: str|None = None,
    repo_name: str|None = None,
    from_path: Path|None = None,
    creator_hostname: str|None = None,
    add_repoyard_ignore: bool = True,
    initialise_git: bool = True,
):
    """
    Create a new repoyard repository.
    """
    ...


# %% [markdown]
# Set up testing args

# %%
# Set up test environment
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = tests_working_dir / "_cmds" / "new"
data_path = test_folder_path / ".repoyard"
# !rm -rf {test_folder_path}

# %%
# Args
config_path = test_folder_path / "repoyard_config" / "config.toml"
storage_location = None
repo_name = "test_repo"
from_path = None
creator_hostname = None
add_repoyard_ignore = True
initialise_git = True

# %%
# Run init
from repoyard._cmds.init import init
init(config_path=config_path, data_path=data_path)

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
    
if storage_location not in config.storage_locations:
    raise ValueError(f"Invalid storage location: {storage_location}. Must be one of: {', '.join(config.storage_locations)}.")
    
if repo_name is None and from_path is None:
    raise ValueError("Either --name or --from must be provided.")

if from_path is not None:
    from_path = Path(from_path).expanduser().resolve()
    
if from_path is not None and repo_name is None:
    repo_name = from_path.name
    
from repoyard._utils import get_hostname
if creator_hostname is None:
    creator_hostname = get_hostname()

# %% [markdown]
# Create meta file

# %%
#|export
from repoyard._repos import RepoMeta
repo_meta = RepoMeta(
    name=repo_name,
    storage_location=storage_location,
    groups=[],
    creator_hostname=creator_hostname,
)
repo_meta.save(config)

# %% [markdown]
# Create the repo folder

# %%
#|export
repo_path = config.included_repostore_path / repo_meta.full_name

if config.storage_locations[storage_location].storage_type != "local":
    if from_path is not None:
        from_path.rename(repo_path)
    else:
        repo_path.mkdir(parents=True, exist_ok=True)
else:
    storage_location_repo_path = config.local_repostore_path / repo_meta.full_name
    if from_path is not None:
        from_path.rename(storage_location_repo_path)
    else:
        storage_location_repo_path.mkdir(parents=True, exist_ok=True)
    repo_path.symlink_to(storage_location_repo_path)
    
if not any(repo_path.iterdir()):
    (repo_path / ".repoyard_ignore").touch()

# %% [markdown]
# Add `.repoyard_ignore`

# %%
#|export
if add_repoyard_ignore:
    (repo_path / ".repoyard_ignore").write_text(const.DEFAULT_REPOYARD_IGNORE)

# %% [markdown]
# Run `git init`

# %%
#|export
if initialise_git:
    subprocess.run(["git", "init"], check=True, cwd=repo_path)

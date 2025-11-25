# %% [markdown]
# # _create_user_symlinks

# %%
#|default_exp cmds._create_user_symlinks
#|export_as_func true

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()

# %%
#|top_export
from pathlib import Path

from repoyard.config import get_config
from repoyard import const


# %%
#|set_func_signature
def create_user_symlinks(
    config_path: Path,
    user_repos_path: Path|None = None,
    user_repo_groups_path: Path|None = None,
):
    """
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
user_repos_path = None
user_repo_groups_path = None

# %%

# %%
# Run init
from repoyard.cmds import new_repo, modify_repometa

# Create a new repo
repo_full_name = new_repo(config_path=config_path, repo_name="test_repo")
modify_repometa(
    config_path=config_path,
    repo_full_name=repo_full_name,
    modifications={
        'groups': ['test_group'],
    }
)

# Add a test group and demand unique repo names in it to test the following
import toml
config_dump = toml.load(config_path)
config_dump['repo_groups'] = {
    'test_group': {
        'repo_title_mode': 'name',
        'unique_repo_names': True,
    }
}
config_path.write_text(toml.dumps(config_dump));

# Create a new repo with the same name, to test the conflict handling when adding it to the same group
from repoyard.cmds._modify_repometa import RepoNameConflict
try:
    repo_full_name2 = new_repo(config_path=config_path, repo_name="test_repo")
    modify_repometa(
        config_path=config_path,
        repo_full_name=repo_full_name2,
        modifications={
            'groups': ['test_group'],
        }
    )
    raise ValueError("Should not happen")
except RepoNameConflict: pass

# %% [markdown]
# # Function body

# %% [markdown]
# Process args

# %%
#|export
config = get_config(config_path)

if user_repos_path is None:
    user_repos_path = config.user_repos_path
if user_repo_groups_path is None:
    user_repo_groups_path = config.user_repo_groups_path

# %% [markdown]
# Refresh the repoyard meta file

# %%
#|export
from repoyard._models import refresh_repoyard_meta
refresh_repoyard_meta(config)

# %%
ps = [p.name for p in config.user_repos_path.glob('*')]
assert repo_full_name in ps

# %% [markdown]
# Create repo group symlinks

# %%
#|export
from repoyard._models import create_user_repo_group_symlinks

create_user_repo_group_symlinks(
    config=config,
)

# %%
assert next(p.name for p in (config.user_repo_groups_path / "test_group").glob('*')) == 'test_repo'

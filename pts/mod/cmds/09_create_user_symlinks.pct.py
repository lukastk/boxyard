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
# Set up test environment
import tempfile
tests_working_dir = const.pkg_path.parent / "tmp_tests"
test_folder_path = Path(tempfile.mkdtemp(prefix="create_user_symlinks", dir="/tmp"))
test_folder_path.mkdir(parents=True, exist_ok=True)
symlink_path = tests_working_dir / "_cmds" / "create_user_symlinks"
symlink_path.parent.mkdir(parents=True, exist_ok=True)
if symlink_path.exists() or symlink_path.is_symlink():
    symlink_path.unlink()
symlink_path.symlink_to(test_folder_path, target_is_directory=True) # So that it can be viewed from within the project working directory
data_path = test_folder_path / ".repoyard"

# %%
# Args
config_path = test_folder_path / "repoyard_config" / "config.toml"
user_repos_path = None
user_repo_groups_path = None

# %%
# Run init
from repoyard.cmds import init_repoyard, new_repo, sync_repo, modify_repometa
init_repoyard(config_path=config_path, data_path=data_path)

# Add a storage location 'my_remote'
import toml
config_dump = toml.load(config_path)
remote_rclone_path = Path(tempfile.mkdtemp(prefix="rclone_remote", dir="/tmp"))
config_dump['user_repos_path'] = (test_folder_path / "user_repos").as_posix()
config_dump['user_repo_groups_path'] = (test_folder_path / "user_repo_groups").as_posix()
config_dump['repo_groups'] = {
    'test_group': {
        'repo_title_mode': 'name',
        'unique_repo_names': True,
    }
}
config_path.write_text(toml.dumps(config_dump));

# Create a new repo
repo_full_name = new_repo(config_path=config_path, repo_name="test_repo")
modify_repometa(
    config_path=config_path,
    repo_full_name=repo_full_name,
    modifications={
        'groups': ['test_group'],
    }
)

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
# Create repo symlinks

# %%
#|export
from repoyard._models import create_user_repos_symlinks, get_repoyard_meta

repoyard_meta = get_repoyard_meta(config)
included_repo_metas = [repo_meta for repo_meta in repoyard_meta.repo_metas if repo_meta.check_included(config)]
create_user_repos_symlinks(
    config=config,
    repo_metas=included_repo_metas,
)

# %%
assert next(p.name for p in config.user_repos_path.glob('*')) == repo_full_name

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

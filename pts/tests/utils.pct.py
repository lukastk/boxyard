# %% [markdown]
# # utils

# %%
#|default_exp utils

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import tests as this_module

# %%
#|export
import subprocess
from pathlib import Path
import shutil
import tempfile
import toml
import inspect

from repoyard import const
from repoyard.cmds import *
from repoyard._models import get_repoyard_meta
from repoyard.config import get_config


# %%
#|export
def create_repoyards(remote_name="my_remote", num_repoyards=1):
    remote_rclone_path = Path(tempfile.mkdtemp(prefix=f"{remote_name}_", dir="/tmp"))

    repoyards = []
    for i in range(num_repoyards):
        test_folder_path = Path(tempfile.mkdtemp(prefix=f"repoyard_{i}_", dir="/tmp"))
        test_folder_path.mkdir(parents=True, exist_ok=True)
        config_path = test_folder_path / ".config" / "repoyard" / "config.toml"
        data_path = test_folder_path / ".repoyard"

        # Run init
        init_repoyard(config_path=config_path, data_path=data_path, verbose=False)
        config = get_config(config_path)

        # Add a storage location
        config_dump = toml.load(config_path)
        config_dump['storage_locations'][remote_name] = {
            'storage_type' : "rclone",
            'store_path' : "repoyard",
        }

        # Set up a rclone remote path
        config.rclone_config_path.write_text(config.rclone_config_path.read_text() + "\n" + inspect.cleandoc(f"""
        [{remote_name}]
        type = alias
        remote = {remote_rclone_path}
        """));

        config_path.write_text(toml.dumps(config_dump))

        # Load config
        config = get_config(config_path)

        repoyards.append((config, config_path, data_path))

    if len(repoyards) == 1:
        config, config_path, data_path = repoyards[0]
        return remote_name, remote_rclone_path, config, config_path, data_path
    else:
        return remote_name, remote_rclone_path, repoyards

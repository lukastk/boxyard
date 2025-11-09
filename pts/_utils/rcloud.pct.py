# %% [markdown]
# # rclone

# %%
#|default_exp utils._rclone

# %%
#|hide
import nblite; from nblite import show_doc; nblite.nbl_export()
import repoyard.utils._rclone as this_module

# %%
#|export
import subprocess

# %%
#|hide
show_doc(this_module.sync)

# %%
subprocess.run(["rclone", '--config', "/Users/lukastk/dev/2025-11-09_00__hetzner-cloud-storage-repoyard-test/rclone.conf"])


# %%
#|export
def sync(
    config_path: strm,
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
):
    source_spec = f"{source}:{source_path}"
    dest_spec = f"{dest}:{dest_path}"
    subprocess.run(["rclone", "sync", '--config', config_path, source_spec, dest_spec])

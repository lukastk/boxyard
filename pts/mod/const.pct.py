# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # const

# %%
#|default_exp const

# %%
#|hide
from nblite import nbl_export, show_doc; nbl_export();

# %%
#|export
from pathlib import Path
import textwrap
import string
from pydantic import BaseModel, ConfigDict
import boxyard as proj

# %%
#|export
pkg_path = Path(proj.__file__).parent

# %% [markdown]
# Default paths

# %%
#|export
DEFAULT_CONFIG_PATH = Path("~") / ".config" / "boxyard" / "config.toml"
DEFAULT_DATA_PATH = Path("~") / ".boxyard"
DEFAULT_USER_BOXES_PATH = Path("~") / "boxes"
DEFAULT_USER_BOX_GROUPS_PATH = Path("~") / "box-groups"

SYNC_RECORDS_REL_PATH = "sync_records"
REMOTE_BOXES_REL_PATH = "boxes"
REMOTE_BACKUP_REL_PATH = "sync_backups"

BOX_DATA_REL_PATH = "data"
BOX_SYNC_BASE_SUFFIX = ".base.json"
"""Suffix of the machine-local fingerprint sidecar that sits beside a part's
`.rec` sync record. Deliberately NOT `.rec`: doctor's interrupted-record scan
globs `*.rec`, and the sidecar must not be mistaken for a sync record."""

BOX_METAFILE_REL_PATH = "boxmeta.toml"
BOX_CONF_REL_PATH = "conf"

# A box's own rclone filter files, inside its CONF part. When present, the
# exclude file REPLACES the global default -- it does not extend it -- so any
# code deciding which patterns apply to a box must check for it first.
RCLONE_INCLUDE_FILENAME = ".rclone_include"
RCLONE_EXCLUDE_FILENAME = ".rclone_exclude"
RCLONE_FILTERS_FILENAME = ".rclone_filters"

# Sidecar file, stored at the root of a box's DATA part, recording which files are
# executable so the +x bit survives sync over backends that can't carry Unix mode
# metadata (e.g. SFTP). See _utils/perms.py. Ships as ordinary synced content.
BOX_PERMS_MANIFEST_REL_PATH = ".boxyard-perms.json"

# --- restic-backed DATA (see _dev/RESTIC-DATA-STORAGE-DESIGN-NOTE.md) --------
#
# A restic-backed box keeps its DATA as a per-box restic repository beside the
# plain parts, NOT in place of `data/`. The two names deliberately do not
# collide: during the migration window a converted box and an unconverted one
# must be distinguishable, and a half-finished conversion must never be
# ambiguous about which format is authoritative.
BOX_RESTIC_REL_PATH = "data.restic"

# A plain JSON file at depth 2, beside `boxmeta.toml`, naming the snapshot the
# remote currently considers current (and the absolute path it was backed up
# from -- restic records the pusher's path and offers no way to normalise it).
#
# It sits at depth 2 so the ONE bulk `lsjson` that `sync-missing-meta` and
# `--skip-unchanged-meta` already run answers "did this box's DATA move" for the
# whole yard at no additional remote calls. The repo's own `snapshots/`
# directory remains the truth; this is a hint, and a stale hint costs one repo
# open, never correctness.
BOX_SNAPSHOT_POINTER_REL_PATH = "data.snapshot"

# Machine-local record of which snapshot this machine last agreed with, under
# `~/.boxyard/`. Never synced -- like the placement records, it is a fact about
# THIS machine.
RESTIC_STATE_REL_PATH = "restic_state"

# The fixed absolute path every machine backs a box up THROUGH, so that every
# machine records the SAME path string in its snapshots. restic records the path
# as GIVEN -- it does not resolve symlinks -- so a symlink at a constant location
# makes `--parent` and `restic diff` work across machines whose checkout roots
# differ. Verified on Linux and on macOS, where /tmp is itself a symlink to
# private/tmp and the path is still recorded verbatim.
#
# Must be a fixed STRING, so it cannot live under $HOME (/home/... vs /Users/...).
# /tmp is world-writable everywhere, so the root is created 0700 and validated as
# a real, self-owned directory before every use -- see `_restic.canonical_root`.
RESTIC_CANONICAL_ROOT = "/tmp/boxyard-restic"

ENV_VAR_BOXYARD_RESTIC = "BOXYARD_RESTIC"  # explicit full path to the restic binary
ENV_VAR_BOXYARD_RESTIC_PASSWORD = "BOXYARD_RESTIC_PASSWORD"

# Wall-clock ceiling for restic calls whose work is bounded: reading the
# snapshot list, diffing two snapshots, resolving a snapshot's source path.
# Backup and restore are NOT bounded by this -- a large box legitimately takes
# a long time, exactly as for rclone transfers.
RESTIC_METADATA_TIMEOUT = 600.0

# restic exit code for "could not acquire the repository lock". `forget` and
# `prune` take an EXCLUSIVE lock, so a concurrent `backup` against the same
# per-box repo hits this. Named rather than spelled 11 at the call site.
RESTIC_EXIT_LOCK_FAILED = 11

SOFT_INTERRUPT_COUNT = 3

# How often the suspend watchdog compares the wall and monotonic clocks, and how
# far they must diverge before we conclude the machine was suspended. The
# threshold only needs to sit above normal clock slew (NTP steps a few seconds at
# most); real sleeps are minutes to hours. See `_utils.base.run_cmd_async`.
SUSPEND_POLL_INTERVAL = 5.0
SUSPEND_DETECT_THRESHOLD = 60.0

# Wall-clock ceiling for rclone calls whose work is inherently bounded (listings
# and metadata reads). Deliberately generous — a large listing over a slow link
# is normal, a listing that runs for ten minutes is not. Transfers are NOT
# bounded by this; see `_utils.base.run_cmd_async`.
RCLONE_LISTING_TIMEOUT = 600.0

DEFAULT_FAKE_STORE_REL_PATH = "fake_store"

# %% [markdown]
# Other constants

# %%
#|export
DEFAULT_RCLONE_EXCLUDE = textwrap.dedent("""
.venv/
.pixi/
.trunk/
node_modules/
__pycache__/

.DS_Store
""").strip()

BOX_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
BOX_TIMESTAMP_FORMAT_DATE_ONLY = "%Y%m%d"
DEFAULT_BOX_SUBID_CHARACTER_SET = string.ascii_lowercase + string.digits
DEFAULT_BOX_SUBID_LENGTH = 5

DEFAULT_MAX_CONCURRENT_RCLONE_OPS = 3

# %%
subid_num = len(DEFAULT_BOX_SUBID_CHARACTER_SET) ** DEFAULT_BOX_SUBID_LENGTH
print(f"Number of possible subids: {subid_num / 1e6} million.\n")

p_no_collide = 1 - (1 / subid_num)
for i in range(2, 7):
    print(
        f"Likelihood of collision if creating 1e{i} boxes with the same name per day:"
    )
    num = 10**i
    print(f"  {1 - p_no_collide**num:.2e}")

# %% [markdown]
# Environment variables

# %%
#|export
ENV_VAR_BOXYARD_CONFIG_PATH = "BOXYARD_CONFIG_PATH"
ENV_VAR_DEFAULT_BOX_GROUPS = "DEFAULT_BOX_GROUPS"
ENV_VAR_BOXYARD_RCLONE = "BOXYARD_RCLONE"  # explicit full path to the rclone binary
ENV_VAR_BOXYARD_MACHINE_NAME = "BOXYARD_MACHINE_NAME"  # overrides `machine_name`

# %% [markdown]
# Machine identity

# %% [markdown]
# A machine name identifies one machine to the whole yard: it is what
# `boxmeta.toml`'s `write_owner` will hold, and it is compared, not printed.
# `get_hostname()` cannot serve — the live yard holds both `lukas-pocket4` and
# `pocket4` for one physical machine, plus macOS pretty names like
# `Lukas’s MacBook Pro` (spaces, a U+2019 apostrophe, user-editable in
# System Settings). So the name is configured, never derived, and constrained
# to characters that survive being compared, printed in an error, and passed
# on a command line.

# %%
#|export
MACHINE_NAME_REGEX = r"[A-Za-z0-9_-]{1,64}"

# %% [markdown]
# Misc

# %%
#|export
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

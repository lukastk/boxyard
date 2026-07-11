## [0.3.1] - 2026-07-11

### 🐛 Bug Fixes

- `delete` now also purges the box's sync-record and sync-backup directories
  (local + remote); previously every delete left orphaned sync records that
  `boxyard doctor` flagged (#13).
- `delete` removes the local box *before* creating the tombstone / purging the
  remote, so a permission failure aborts cleanly instead of leaving a partial
  delete; and a file owned by another user now raises an actionable
  `chown` hint instead of a raw traceback (#15).
- `remove-parent --parent-id <id>` can now drop a dangling parent whose box has
  been deleted (the exact fix `boxyard doctor` recommends for `tree-orphans`);
  previously it errored with "Box with id … not found" (#14).

## [0.3.0] - 2026-07-10

### 🚀 Features

- Preserve the executable bit (`+x`) across sync. rclone drops Unix mode on
  transfer and the SFTP backend can't carry mode metadata at all, so `+x` was
  lost on every round-trip. Boxes now carry a `.boxyard-perms.json` manifest at
  the DATA root recording which files are executable; it is (re)generated before
  a push and re-applied after a pull. Always-on. v1 is additive-only (restores
  `+x`, never clears it) so mixed old/new client versions stay safe during
  rollout. New module `boxyard._utils.perms`.

### 🔧 Tooling

- Pin `nblite>=1.2.2` — 1.2.1 emits broken relative imports for function-export
  modules.

## [0.1.8] - 2025-11-19

### 🚀 Features

- Improved the results UI for multi-sync
- Lowered the DEFAULT_MAX_CONCURRENT_RCLONE_OPS
- Added --pick-first option to CLI 'path'
- Removed 'sync_repometas' and replaced it with 'sync_missing_repometas'
- Option --no-print-skipped in multi-sync
- 'create_user_symlinks' now reloads the repoyard_meta file before doing anything
- Include symlinks in rclone_lsjson
- Improved prints
- Changed default NameMatchMode for 'path'
- Optimised 'check_last_time_modified'
- Allow for nested repo groups now in the user repo group symlink creation
- List-groups can now be targeted towards specific repos
- Default repo groups

### 🐛 Bug Fixes

- Had not pushed these. the multi-sync cli with better UI
- Missing dev dependency
- Typo in 'new' cli
- Normalise 'group' flags in cli
- References to non-existing function get_repo_full_name_from_cwd
- 'create_repoyard_meta' was trying to any file in the local store as a repometa (e.g. '.DS_Store')
- Improved the 'get_sync_status' logic
- Typo
- Bugs in 'get_sync_status'
- 'exclude' now checks if the repo is local
- Proper validation on group names and allow for '/' in the group filters

### 🚜 Refactor

- Headline

### ⚙️ Miscellaneous Tasks

- Uv.lock
- Update version in pyproject.toml
## [0.1.7] - 2025-11-16

### 🐛 Bug Fixes

- Typo SyncRecord.creator_hostname -> SyncRecord.syncer_hostname

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Update version in pyproject.toml
## [0.1.6] - 2025-11-16

### 🚀 Features

- The global default.repoyard_exclude file is now used by default if no exclude file is present

### 🐛 Bug Fixes

- Symlinks for fake stores
- Typo in numbering
- Added symlink syncing

### 🚜 Refactor

- Changed '.repoyard_*' files to '.rclone_' files. more fitting

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Update version in pyproject.toml
## [0.1.5] - 2025-11-16

### 🚀 Features

- 'get_sync_status' now raises an exception if there is no local sync record, but there is a remote folder. this indicates that the sync was aborted.
- Safer syncing. syncing now creates an intermediate sync record signifying an ongoing sync, and also backups the synced files temporarily.
- Enabled 'soft interruption', intercepting OS signals to delay termination until it is safe to do so.
- Soft interruption will now force interrupt if 3 signals are sent
- Better in-progress output for multi-sync

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Update version in pyproject.toml
## [0.1.4] - 2025-11-16

### 🚀 Features

- Supports specifying the creation timestamp
- 'create_user_repo_group_symlinks' now removes empty folders that do not correspond to repo groups in the user repo group folder
- Can now pass paths instead of specifying repo names to certain CLI functions

### 🐛 Bug Fixes

- Creation timestamp specification wasn't working
- Typo in 'new'
- Removed some tests now that bisync is no longer supported
- Repo names were wrongly parsed from paths
- Cmds had changed

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Ran nbl prepare with new nblite version
- Update version in pyproject.toml
## [0.1.3] - 2025-11-16

### 🚀 Features

- Implemented new syncing system based on 'rclone sync' rather than 'rclone bisync'
- 'copy_from_path' in 'new_repo'
- RepoMeta.get_user_path
- Fuzzy string matching in repo_name and other modes
- Changed the form of repo ids to a more legible form
- Async support
- 'max_concurrent_rclone_ops' in cli 'sync-meta'
- CLI commands 'list', 'yard-status' and 'multi-sync'
- Enabled support for unique names in repo groups
- Create_user_symlinks
- Changed default
- Cli create-user-symlinks
- 'create_user_repos_symlinks' now removes old symlinks
- User symlink creation does now not delete symlinks that are meant to be there
- CLI commands now creates user symlinks automatically
- Logical expression filtering of groups
- Optimised group filtering

### 🐛 Bug Fixes

- 'new' doesnt reinitialise .git now
- Expanduser issues
- 'get_repo_full_name_from_sub_path' now resolves symlink paths
- Error in checking 'res_dry'
- Removed deprectated 'bisync_helper'
- Properly implemented repo_full_names and storage_locations filter
- Removed dud arguments
- Updated CLIs for the new cmd functions
- 'get_local_sync_record_path' wasn't used in '02_get_repo_sync_status'
- Removed the direction in SyncRecords as it was inconsistent with the approach
- Allow for __init__ in tests/
- Fixed SyncRecord logic
- Typo
- Removed old SyncConfig
- Missing await
- Async_throttler now propagates exceptions
- Create_user_symlinks was async
- Missing import
- Better repo name conflict handling

### 🚜 Refactor

- More sensible default
- Removed dud files
- Fixed import

### 🧪 Testing

- Test.utils
- Finished tests 00 and 01
- Run_cmd
- Test_02_remote
- Fixed test_02_remote

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Added module code so that you can install directly from git
- Uv.lock
- Nbl prepare
- Python-dotenv dev-dependency
- Uv.lock
- Update version in pyproject.toml
## [0.1.2] - 2025-11-14

### 🐛 Bug Fixes

- Was config.json instead of config.toml

### ⚙️ Miscellaneous Tasks

- Update CHANGELOG.md
- Update version in pyproject.toml
## [0.1.1] - 2025-11-14

### 🐛 Bug Fixes

- Issues with '~' in paths

### ⚙️ Miscellaneous Tasks

- Git-cliff
- Docs defined in nblite.toml
- Update version in pyproject.toml
## [0.1.0] - 2025-11-13

### ⚙️ Miscellaneous Tasks

- Updated nblite
- Updated nblite
- Publish script
- Update version in pyproject.toml

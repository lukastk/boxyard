## [0.4.2] - 2026-08-22

### 🐛 Bug Fixes

- **Same-named boxes were silently dropped from a group's symlink tree.** When
  two boxes resolved to the same title in a group, the CONFLICT suffix that was
  supposed to disambiguate them never did. Two compounding faults: the threshold
  was `> 1`, but the counter holds how many boxes have *already* taken the
  title, so the second box to want `"foo"` saw `1`, failed the test, and took
  `"foo"` as well; and the increment landed on the *rewritten* key, so once a
  box became `"foo (CONFLICT 2)"` the count for `"foo"` stopped rising and every
  later box computed that same suffix.

  N boxes sharing a title therefore produced only **two** distinct names, and
  symlink creation resolved each collision last-one-wins — so five same-named
  boxes yielded two symlinks and three boxes were simply absent from the group,
  with no warning. Since every `active/*` group uses
  `box_title_mode = "name"`, this meant real work could quietly go missing
  from `~/g`.

  Numbering is now sequential and every box gets its own symlink: `foo`,
  `foo (CONFLICT 1)`, `foo (CONFLICT 2)`, …

## [0.4.1] - 2026-08-22

Two more silent failures, of the same family as the v0.4.0 permissions bug: a
swallowed error producing wrong behaviour instead of an error.

### 🐛 Bug Fixes

- **A box with changes under an unreadable directory was never pushed.**
  `check_last_time_modified` swallowed every `OSError` from `os.scandir`. That
  walk answers "when did this box last change?", and the answer drives the sync
  decision — so those changes lowered nothing, the box reported an older mtime,
  looked `SYNCED`, and was silently left unsynced. It now raises, naming the
  directory to fix. A directory or file that *vanishes* mid-walk is a real race
  and is still tolerated.

- **A remote sync record disappearing mid-pull raised an opaque
  `AttributeError`.** `sync_helper` read the remote record after a successful
  pull and called `.rclone_save` on it without a `None` check, so a box deleted
  from another machine mid-sync produced `'NoneType' object has no attribute
  'rclone_save'`. It now raises `SyncFailed` with an explanation. The local
  record is already incomplete at that point, so the next run sees
  `SYNC_FROM_REMOTE_INCOMPLETE` and can safely retry.

## [0.4.0] - 2026-08-22

Nine bugs, found by building a second implementation and comparing the two.
Most were silent: they produced wrong state rather than an error, which is
exactly the failure mode this codebase's "loud errors, never silent fallbacks"
rule exists to prevent.

### 🐛 Bug Fixes

- **The CLI ignored `BOXYARD_CONFIG_PATH`.** The entrypoint read the `--config`
  flag and otherwise went straight to the default, while the variable was
  consulted only when resolving `rclone_path` and in an `init` message telling
  the user to set it. So `boxyard init --config <path>` instructed you to set a
  variable that every subsequent command then discarded, silently operating on
  the default config. Resolution is now flag, then env var, then default.

- **`boxyard init` ignored the global `--config` too**, keeping its own
  `--config-path` and defaulting independently of it. `--config-path` still
  wins when given, but it now falls back to the same resolution as every other
  command.

- **A shrunken permissions manifest could permanently lose exec bits.**
  `os.walk` swallowed unreadable directories, so every file beneath one was
  silently dropped from `.boxyard-perms.json`; the shrunken manifest was then
  pushed, and every machine that pulled it lost the `+x` bits it no longer
  mentioned. Walk errors now raise. A file vanishing between `readdir` and
  `stat` is still tolerated — that race is real.

- **The permissions manifest could chmod paths outside the box.**
  `apply_exec_manifest` did not reject entries that were absolute or contained
  `..`, and `pathlib` join lets an absolute entry *replace* the root. Manifests
  arrive from a shared remote, so entries are now validated — all of them,
  before any chmod, so a bad manifest cannot half-apply. A corrupt manifest
  raises rather than warning; an absent one remains a legitimate no-op.

- **The manifest listed non-regular files.** `_iter_regular_files` excluded
  only symlinks, but `os.walk` also yields fifos, sockets and devices. rclone
  does not transfer those, so their exec bits were pure noise.

- **A box lock could escape the locks directory.** `box_sync_lock_path` did no
  validation, so an index name containing `/` nested the lock inside a tree and
  one containing `..` placed it outside `~/.boxyard/locks` entirely. Index
  names are now validated as a single path component — deliberately more
  leniently than `validate_box_name`, since box names went unvalidated before
  v0.3.3 and legacy boxes must still be lockable.

- **`cleanup_stale_locks` always returned `[]`.** `filelock`'s own `release()`
  unlinks the lock file, so the following `unlink()` raised `FileNotFoundError`
  — which a blanket `except OSError` swallowed, taking the `removed.append()`
  on the next line with it. Files *were* deleted; callers were told nothing
  had been. `auto_cleanup_stale_locks(verbose=True)` could therefore never
  print anything. Only "the file disappeared underneath us" is tolerated now.

- **A malformed `filter_expr` did not fail at config load.**
  `get_group_filter_func` only tokenizes eagerly and re-parses on every call,
  so `"(a AND b"` compiled fine and raised only when the virtual group was
  first evaluated — during symlink building, far from the typo that caused it.

- **`validate_group_name` accepted a trailing newline.** Python's `$` also
  matches immediately before one, so `"proj\n"` passed and would have been used
  verbatim as a directory name in the group tree. Now uses `re.fullmatch`.

- **`BoxMeta.create` was broken whenever an explicit timestamp was passed** —
  for every input type, a `TypeError` for a datetime and an `AttributeError`
  for a string. Unreachable, because `new_box` builds its `BoxMeta` directly,
  so nothing ever caught it.

### 🔧 Changes

- **Replaced the `toml` dependency with `tomllib` + `tomli_w`.** `toml` 0.10.2
  has been unmaintained since 2020 and silently corrupts control characters:
  dumping `"del\x7fchar"` produced `"delx7fchar"`, round-tripping back to the
  wrong string with no error. `tomllib` is in the standard library on the
  Python versions boxyard supports, which as a side effect makes `_fast.py`
  genuinely dependency-free for the first time.

  Written TOML now formats lists one per line rather than `[ "a", "b",]`. This
  is cosmetic and causes no resync: `boxmeta.toml` is only rewritten when a box
  is modified, and both formats parse.

- `creator_hostname` now rejects control characters. This began as a workaround
  for the `toml` corruption above and is kept on its own merits: such a
  character in a machine name is meaningless and would corrupt the output of
  `list` and `doctor`, which print it.

## [0.3.3] - 2026-08-11

### 🐛 Bug Fixes

- A single bad box name no longer bricks the whole yard. A box's `index_name`
  (`{box_id}__{name}`) is interpolated straight into filesystem paths, so a name
  that was not a single path component spread the box over a nested directory
  tree — `boxyard new -n "/github.com/user/repo"` created
  `local_store/<loc>/<box_id>__/github.com/user/repo/boxmeta.toml`. The top
  level of that tree, `<box_id>__`, then looked like a box registration with no
  `boxmeta.toml`, and since `create_boxyard_meta` scans registrations at depth 1
  and let `BoxMeta.load` raise, *every* later command that refreshed the meta
  died — for all boxes, permanently, until the directory was removed by hand.

  Three independent fixes, each of which alone would have prevented the outage:

  - **Box names are validated.** `validate_box_name` rejects anything that is
    not a single path component (separators, `.`/`..`, a leading dot,
    leading/trailing whitespace, empty, NUL), and `new_box` / `rename_box` call
    it before any of the box exists.
  - **The meta refresh survives an unreadable registration.**
    `create_boxyard_meta` now reports each one it cannot load on stderr,
    pointing at `boxyard doctor`, and builds the meta from the rest instead of
    failing the whole yard.
  - **Box creation is transactional.** Writing the boxmeta, creating the
    directories, cloning or moving in the contents, and `git init` now run under
    one rollback: on failure the box directories are removed (a moved-in
    `from_path` is put back where it came from), the global lock is released,
    and the error is re-raised. Previously a failed `git clone` left a
    fully-registered box behind, so each retry silently added another duplicate.

## [0.3.2] - 2026-07-27

### 🐛 Bug Fixes

- Sync no longer wedges forever when the machine sleeps mid-sync. rclone
  children spawned just before a suspend come back with dead TCP connections
  and can spin at 100% CPU indefinitely — observed as two `lsjson` processes
  burning a core each for 9.5 hours, with no open sockets. Because
  `run_cmd_async` awaited `communicate()` with no timeout, both `multi-sync`
  concurrency slots stayed occupied, the run never returned, and the supervisor
  loop never started another cycle — so *no box synced at all* until the
  processes were killed by hand.

  Two guards now cover this:

  - **A suspend watchdog.** `time.monotonic()` does not advance while the system
    is suspended but `time.time()` does, so a divergence between them means the
    machine slept and every in-flight rclone child is holding a dead
    connection. Those children are killed and the caller fails loudly and
    retries on fresh connections. This deliberately does *not* penalise long
    transfers: a multi-hour push is untouched unless it spanned a suspend, in
    which case it was already doomed.
  - **A timeout on bounded operations.** `lsjson`, `mkdir`, `cat` and
    `path-exists` do inherently finite work, so they now take a wall-clock
    ceiling (`RCLONE_LISTING_TIMEOUT`, 10 min) and raise `CommandTimeout`
    rather than hanging. Transfers stay unbounded, since no wall-clock limit is
    meaningful for them.

  Note that rclone's own `--timeout` (5m IO idle), `--contimeout` (1m) and
  `--sftp-idle-timeout` (1m) were all in effect during the incident and none of
  them fired, which is why the guard has to live on the boxyard side.

- Subprocesses are now spawned in their own session and killed by process
  group, so an rclone that has spawned children cannot leave orphans behind
  when it is timed out or killed.

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

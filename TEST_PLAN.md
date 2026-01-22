# Test Plan for Repoyard

This document outlines a comprehensive testing strategy for the repoyard project, including test organization, coverage goals, and implementation details.

## Table of Contents

1. [Test Organization](#test-organization)
2. [Test Categories](#test-categories)
3. [Phase 1: Logical Expressions](#phase-1-logical-expressions)
4. [Phase 2: Data Models](#phase-2-data-models)
5. [Phase 3: Configuration](#phase-3-configuration)
6. [Phase 4: Utilities](#phase-4-utilities)
7. [Phase 5: Sync Logic](#phase-5-sync-logic)
8. [Phase 6: Commands](#phase-6-commands)
9. [Phase 7: Integration Tests](#phase-7-integration-tests)
10. [Implementation Notes](#implementation-notes)

---

## Test Organization

### Directory Structure

Tests are organized by the module/component they test, mirroring the source structure:

```
pts/tests/
├── unit/                              # Unit tests (no external resources)
│   ├── _utils/                        # Tests for _utils/ module
│   │   ├── test_logical_expressions.pct.py
│   │   ├── test_base_utils.pct.py
│   │   └── test_rclone_cmd_builder.pct.py
│   │
│   ├── models/                        # Tests for _models.py
│   │   ├── test_repo_meta.pct.py
│   │   ├── test_repoyard_meta.pct.py
│   │   ├── test_sync_record.pct.py
│   │   └── test_sync_status.pct.py
│   │
│   ├── config/                        # Tests for config.py
│   │   ├── test_config_loading.pct.py
│   │   ├── test_config_validation.pct.py
│   │   ├── test_storage_config.pct.py
│   │   └── test_group_config.pct.py
│   │
│   └── cmds/                          # Tests for command logic
│       ├── test_modify_repometa.pct.py
│       └── test_repo_creation.pct.py
│
├── integration/                       # Integration tests (require rclone)
│   ├── sync/                          # Sync workflow tests
│   │   ├── test_basic_sync.pct.py     # (migrate from test_00_sync)
│   │   ├── test_multi_machine.pct.py  # (migrate from test_01_multiple_locals)
│   │   └── test_conflict_resolution.pct.py
│   │
│   ├── remote/                        # Remote operations
│   │   └── test_remote_ops.pct.py     # (migrate from test_02_remote)
│   │
│   ├── symlinks/                      # Symlink creation tests
│   │   └── test_symlink_creation.pct.py
│   │
│   └── groups/                        # Group management tests
│       └── test_group_management.pct.py
│
├── conftest.pct.py                    # Shared fixtures
└── utils.pct.py                       # Test utilities (existing)
```

### Notebook Format

- **Standard tests**: Regular notebooks with `#|export` cells
- **Long tests**: Use function export mode (`#|export_as_func true`) for complex test scenarios that benefit from being a single callable function
- **Shared fixtures**: `conftest.pct.py` exports to pytest's conftest.py

### Markers

```python
@pytest.mark.unit          # No I/O, no external resources, fast
@pytest.mark.integration   # Requires rclone setup, creates temp files
@pytest.mark.slow          # Long-running tests
```

---

## Test Categories

### Priority Levels

| Priority | Category | Description |
|----------|----------|-------------|
| P0 | Critical | Core logic that must work correctly (sync status, expressions) |
| P1 | High | Data models, config validation, path generation |
| P2 | Medium | Commands, symlink creation, utilities |
| P3 | Low | Edge cases, error messages, CLI formatting |

### Coverage Goals

| Component | Target Coverage | Notes |
|-----------|-----------------|-------|
| `_utils/logical_expressions.py` | 95%+ | Complex parser with many branches |
| `_models.py` | 90%+ | Core data structures |
| `config.py` | 85%+ | Configuration validation |
| `_utils/base.py` | 70%+ | Many I/O-dependent functions |
| `_utils/sync_helper.py` | 80%+ | Critical sync logic |
| `cmds/` | 60%+ | Integration-heavy |

---

## Phase 1: Logical Expressions

**File**: `pts/tests/unit/_utils/test_logical_expressions.pct.py`
**Priority**: P0 (Critical)
**Module Under Test**: `repoyard._utils.logical_expressions`

### Test Cases

#### 1.1 Basic Operators

```python
# Single group
test_single_group_match()
test_single_group_no_match()

# AND operator
test_and_both_present()
test_and_one_missing()
test_and_both_missing()

# OR operator
test_or_both_present()
test_or_one_present()
test_or_neither_present()

# NOT operator
test_not_present()
test_not_absent()
```

#### 1.2 Operator Precedence

```python
# NOT > AND > OR precedence
test_precedence_not_binds_tighter_than_and()     # "NOT a AND b" == "(NOT a) AND b"
test_precedence_and_binds_tighter_than_or()      # "a OR b AND c" == "a OR (b AND c)"
test_precedence_complex()                         # "NOT a OR b AND c" == "(NOT a) OR (b AND c)"
```

#### 1.3 Parentheses

```python
test_parens_override_precedence()     # "(a OR b) AND c"
test_nested_parens()                   # "((a OR b) AND c)"
test_parens_with_not()                 # "NOT (a AND b)"
test_complex_nesting()                 # "(a AND (b OR c)) OR (NOT d AND e)"
```

#### 1.4 Whitespace Handling

```python
test_extra_spaces()                    # "a  AND  b"
test_leading_trailing_spaces()         # "  a AND b  "
test_no_spaces()                       # "a AND b" (minimum required)
test_tabs_and_newlines()               # Various whitespace characters
```

#### 1.5 Case Insensitivity (Operators)

```python
test_lowercase_operators()             # "a and b", "a or b", "not a"
test_mixed_case_operators()            # "a And b", "a Or b", "Not a"
test_uppercase_operators()             # "a AND b" (standard)
```

#### 1.6 Group Name Handling

```python
test_simple_group_names()              # "backend", "api"
test_hyphenated_names()                # "my-group"
test_underscored_names()               # "my_group"
test_slashed_names()                   # "category/subcategory"
test_numeric_names()                   # "v2", "2024"
test_mixed_names()                     # "my-group_v2/prod"
```

#### 1.7 Error Cases

```python
test_empty_expression_raises()
test_unmatched_open_paren_raises()
test_unmatched_close_paren_raises()
test_trailing_operator_raises()        # "a AND"
test_leading_operator_raises()         # "AND a"
test_double_operator_raises()          # "a AND AND b"
test_invalid_characters_raises()       # "a & b", "a | b"
```

#### 1.8 Complex Real-World Expressions

```python
test_backend_not_deprecated()          # "backend AND NOT deprecated"
test_api_or_frontend()                 # "api OR frontend"
test_complex_filter()                  # "(backend OR frontend) AND NOT (deprecated OR legacy)"
test_deeply_nested()                   # "((a AND b) OR (c AND d)) AND NOT ((e OR f) AND g)"
```

#### 1.9 Edge Cases

```python
test_group_name_is_operator_word()     # Group named "and", "or", "not"
test_single_char_groups()              # "a AND b"
test_empty_group_list()                # Filter func with empty list input
test_all_groups_match()
test_no_groups_match()
```

---

## Phase 2: Data Models

### 2.1 RepoMeta

**File**: `pts/tests/unit/models/test_repo_meta.pct.py`
**Priority**: P1 (High)

#### Test Cases

```python
# Creation
test_create_generates_valid_repo_id()
test_create_generates_unique_subids()
test_create_uses_correct_timestamp_format()
test_create_with_date_only_format()
test_create_sets_creator_hostname()
test_create_with_default_groups()

# Properties
test_repo_id_format_datetime()         # "20251122_143022_a7kx9"
test_repo_id_format_date_only()        # "20251122_a7kx9"
test_index_name_format()               # "{repo_id}__{name}"
test_index_name_with_special_chars()   # Name with spaces, etc.
test_creation_timestamp_datetime_property()

# Path Generation
test_get_local_path()
test_get_remote_path()
test_get_local_part_path_data()
test_get_local_part_path_meta()
test_get_local_part_path_conf()
test_get_remote_part_path_data()
test_get_remote_part_path_meta()
test_get_remote_part_path_conf()
test_get_local_sync_record_path()
test_get_remote_sync_record_path()

# Validation
test_validate_group_name_valid()       # alphanumeric, _, -, /
test_validate_group_name_invalid()     # spaces, special chars
test_validate_group_name_empty()
test_validate_unique_groups()

# Serialization
test_save_creates_toml_file()
test_load_reads_toml_file()
test_save_load_roundtrip()
test_load_missing_file_returns_none()
test_load_invalid_toml_raises()
```

### 2.2 RepoyardMeta

**File**: `pts/tests/unit/models/test_repoyard_meta.pct.py`
**Priority**: P1 (High)

```python
# Index Properties
test_by_storage_location_index()
test_by_id_index()
test_by_index_name_index()
test_indexes_are_cached()
test_indexes_with_empty_list()
test_indexes_with_duplicates()         # Same repo_id shouldn't happen, but test behavior

# Index Lookups
test_lookup_by_storage_location()
test_lookup_by_id()
test_lookup_by_index_name()
test_lookup_missing_key()
```

### 2.3 SyncRecord

**File**: `pts/tests/unit/models/test_sync_record.pct.py`
**Priority**: P1 (High)

```python
# Creation
test_create_generates_ulid()
test_create_sets_incomplete()
test_create_sets_hostname()
test_ulid_is_sortable_by_time()

# Properties
test_timestamp_extracted_from_ulid()

# Serialization (mock rclone)
test_serialize_format()
test_deserialize_format()
```

### 2.4 Sync Status

**File**: `pts/tests/unit/models/test_sync_status.pct.py`
**Priority**: P0 (Critical)

This tests the `get_sync_status()` function logic (mocking filesystem/rclone).

```python
# SYNCED condition
test_synced_both_exist_same_ulid_no_changes()
test_synced_neither_exists()

# NEEDS_PUSH condition
test_needs_push_local_modified_after_sync()
test_needs_push_local_exists_remote_missing()

# NEEDS_PULL condition
test_needs_pull_remote_newer_ulid()
test_needs_pull_local_excluded()

# CONFLICT condition
test_conflict_both_modified()
test_conflict_different_ulids_local_modified()

# SYNC_INCOMPLETE condition
test_incomplete_local_record_incomplete()
test_incomplete_remote_record_incomplete()

# EXCLUDED condition
test_excluded_remote_exists_local_missing()

# ERROR condition
test_error_remote_exists_no_record()
test_error_path_type_mismatch()
test_error_permission_denied()
```

---

## Phase 3: Configuration

### 3.1 Config Loading

**File**: `pts/tests/unit/config/test_config_loading.pct.py`
**Priority**: P1 (High)

```python
# Basic Loading
test_load_valid_config()
test_load_minimal_config()
test_load_full_config()
test_load_missing_file_raises()
test_load_invalid_toml_raises()

# Path Expansion
test_tilde_expansion()                 # ~/repos -> /home/user/repos
test_relative_path_expansion()

# Default Values
test_default_storage_location_applied()
test_default_paths_applied()
test_default_concurrent_ops_applied()
test_default_subid_length_applied()
```

### 3.2 Config Validation

**File**: `pts/tests/unit/config/test_config_validation.pct.py`
**Priority**: P1 (High)

```python
# Storage Location Validation
test_valid_storage_names()
test_invalid_storage_name_special_chars()
test_invalid_storage_name_spaces()
test_default_storage_must_exist()
test_default_storage_missing_raises()

# Group Name Validation
test_valid_group_names_in_config()
test_invalid_group_names_raise()
test_virtual_group_names_validated()

# Cross-Validation
test_no_overlap_regular_virtual_groups()
```

### 3.3 Storage Config

**File**: `pts/tests/unit/config/test_storage_config.pct.py`
**Priority**: P2 (Medium)

```python
test_rclone_storage_type()
test_local_storage_type()
test_store_path_expansion()
test_store_path_relative_resolved()
```

### 3.4 Group Config

**File**: `pts/tests/unit/config/test_group_config.pct.py`
**Priority**: P2 (Medium)

```python
# RepoGroupConfig
test_default_symlink_name()
test_custom_symlink_name()
test_title_mode_index_name()
test_title_mode_datetime_and_name()
test_title_mode_name()

# VirtualRepoGroupConfig
test_filter_expr_parsing()
test_is_in_group_true()
test_is_in_group_false()
test_invalid_filter_expr_raises()
```

---

## Phase 4: Utilities

### 4.1 Base Utilities

**File**: `pts/tests/unit/_utils/test_base_utils.pct.py`
**Priority**: P2 (Medium)

```python
# Subsequence Matching
test_is_subsequence_match_simple()
test_is_subsequence_match_full()
test_is_subsequence_match_partial()
test_is_subsequence_match_no_match()
test_is_subsequence_match_empty_pattern()
test_is_subsequence_match_case_sensitive()

# Hostname Detection
test_get_hostname_returns_string()
test_get_hostname_not_empty()

# Path Utilities
test_get_repo_index_name_from_sub_path()
test_get_repo_index_name_not_in_repo()

# File Counting
test_count_files_in_dir_empty()
test_count_files_in_dir_with_files()
test_count_files_in_dir_recursive()
test_count_files_in_dir_with_exclusions()
```

### 4.2 rclone Command Builder

**File**: `pts/tests/unit/_utils/test_rclone_cmd_builder.pct.py`
**Priority**: P2 (Medium)

```python
# Command Building (test _rclone_cmd_helper indirectly)
test_sync_command_basic()
test_sync_command_with_backup()
test_sync_command_with_exclude()
test_sync_command_with_include()
test_sync_command_with_filter_file()
test_sync_command_with_dry_run()
test_sync_command_with_progress()
test_copy_command()
test_lsjson_command()
```

---

## Phase 5: Sync Logic

### 5.1 Sync Helper

**File**: `pts/tests/unit/_utils/test_sync_helper.pct.py`
**Priority**: P0 (Critical)

Note: This will require significant mocking of rclone operations.

```python
# Direction Inference (CAREFUL mode)
test_infer_direction_needs_push()
test_infer_direction_needs_pull()
test_infer_direction_synced()
test_infer_direction_conflict_raises()

# Safety Checks (CAREFUL mode)
test_careful_rejects_conflict()
test_careful_allows_needs_push()
test_careful_allows_needs_pull()
test_careful_allows_synced()

# REPLACE mode
test_replace_allows_overwrite()

# FORCE mode
test_force_ignores_conflict()
test_force_pushes_regardless()

# Sync Record Creation
test_creates_incomplete_record_before_sync()
test_marks_record_complete_after_success()
test_leaves_incomplete_on_failure()
```

---

## Phase 6: Commands

### 6.1 Modify RepomMeta

**File**: `pts/tests/unit/cmds/test_modify_repometa.pct.py`
**Priority**: P2 (Medium)

```python
# Group Management
test_add_group_to_repo()
test_remove_group_from_repo()
test_add_duplicate_group_ignored()
test_remove_nonexistent_group_ignored()

# Name Changes
test_rename_repo()
test_rename_validates_unique_in_groups()

# Validation
test_virtual_group_rejected()
test_invalid_group_name_rejected()
test_nonexistent_repo_raises()
```

### 6.2 Repo Creation

**File**: `pts/tests/unit/cmds/test_repo_creation.pct.py`
**Priority**: P2 (Medium)

```python
# new_repo logic (mocked filesystem)
test_creates_metadata_file()
test_creates_conf_directory()
test_generates_unique_id()
test_applies_default_groups()
test_validates_storage_location()
```

---

## Phase 7: Integration Tests

### 7.1 Basic Sync Workflows

**File**: `pts/tests/integration/sync/test_basic_sync.pct.py`
**Priority**: P1 (High)
**Migrated From**: `test_00_sync.pct.py`

```python
# Use function export mode for this comprehensive test

test_create_and_push()
test_push_and_pull()
test_modify_and_sync()
test_exclude_and_include()
test_delete_repo()
```

### 7.2 Multi-Machine Sync

**File**: `pts/tests/integration/sync/test_multi_machine.pct.py`
**Priority**: P1 (High)
**Migrated From**: `test_01_multiple_locals.pct.py`

```python
# Use function export mode

test_two_machines_sequential_sync()
test_two_machines_conflict_detection()
test_conflict_resolution_with_force()
```

### 7.3 Conflict Resolution

**File**: `pts/tests/integration/sync/test_conflict_resolution.pct.py`
**Priority**: P2 (Medium)

```python
test_careful_mode_rejects_conflict()
test_replace_mode_overwrites()
test_force_mode_ignores_state()
test_conflict_after_exclude_include()
```

### 7.4 Remote Operations

**File**: `pts/tests/integration/remote/test_remote_ops.pct.py`
**Priority**: P2 (Medium)
**Migrated From**: `test_02_remote.pct.py`

```python
test_sync_missing_meta()
test_include_from_remote()
test_remote_only_operations()
```

### 7.5 Symlink Creation

**File**: `pts/tests/integration/symlinks/test_symlink_creation.pct.py`
**Priority**: P2 (Medium)

```python
test_creates_group_directories()
test_creates_symlinks_to_repos()
test_title_mode_index_name()
test_title_mode_datetime_and_name()
test_title_mode_name_only()
test_cleans_up_old_symlinks()
test_handles_broken_symlinks()
test_removes_empty_group_dirs()
test_virtual_group_filtering()
```

### 7.6 Group Management

**File**: `pts/tests/integration/groups/test_group_management.pct.py`
**Priority**: P2 (Medium)

```python
test_add_to_group_updates_symlinks()
test_remove_from_group_updates_symlinks()
test_virtual_group_membership()
test_group_filter_in_list_command()
```

---

## Implementation Notes

### Notebook Structure

Standard test notebook:
```python
# %%
#|default_exp tests.unit._utils.test_logical_expressions

# %%
#|export
import pytest
from repoyard._utils.logical_expressions import get_group_filter_func

# %%
#|export
def test_single_group_match():
    filter_func = get_group_filter_func("backend")
    assert filter_func(["backend", "api"]) == True

# %%
#|export
def test_single_group_no_match():
    filter_func = get_group_filter_func("backend")
    assert filter_func(["api", "frontend"]) == False

# ... more tests
```

### Function Export Mode (Long Tests)

For complex integration tests:
```python
# %%
#|default_exp tests.integration.sync.test_basic_sync
#|export_as_func true

# %%
#|top_export
import pytest
import tempfile
import asyncio
from pathlib import Path

# %%
#|top_export
@pytest.mark.integration
def test_basic_sync():
    asyncio.run(_test_basic_sync())

# %%
#|set_func_signature
async def _test_basic_sync():
    """Test basic sync workflow: create, push, modify, pull."""
    ...

# %%
#|export
# Setup
with tempfile.TemporaryDirectory() as tmpdir:
    config = create_test_config(tmpdir)
    # ... test implementation
```

### Fixtures (conftest.pct.py)

```python
# %%
#|default_exp tests.conftest

# %%
#|export
import pytest
import tempfile
from pathlib import Path

# %%
#|export
@pytest.fixture
def temp_config():
    """Create a temporary config for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "config.toml"
        # Write minimal config
        yield config_path

# %%
#|export
@pytest.fixture
def mock_rclone(monkeypatch):
    """Mock rclone operations for unit testing."""
    # Implementation
    pass
```

### Running Tests

```bash
# All unit tests
uv run pytest -v -m "unit"

# All tests except integration
uv run pytest -v -m "not integration"

# Specific test file
uv run pytest -v src/tests/unit/_utils/test_logical_expressions.py

# With coverage
uv run pytest --cov=repoyard --cov-report=html -m "not integration"
```

---

## Migration Plan

### Step 1: Create Directory Structure
1. Create `pts/tests/unit/` and subdirectories
2. Create `pts/tests/integration/` and subdirectories
3. Create `pts/tests/conftest.pct.py`

### Step 2: Migrate Existing Tests
1. Move `test_00_sync.pct.py` → `integration/sync/test_basic_sync.pct.py`
2. Move `test_01_multiple_locals.pct.py` → `integration/sync/test_multi_machine.pct.py`
3. Move `test_02_remote.pct.py` → `integration/remote/test_remote_ops.pct.py`
4. Keep `utils.pct.py` as shared utilities

### Step 3: Implement Unit Tests (Priority Order)
1. **Week 1**: Logical expressions (P0)
2. **Week 1-2**: Sync status logic (P0)
3. **Week 2**: RepoMeta and RepoyardMeta (P1)
4. **Week 2-3**: Config loading and validation (P1)
5. **Week 3**: Base utilities (P2)
6. **Week 3-4**: Command logic (P2)

### Step 4: Expand Integration Tests
1. Symlink creation tests
2. Group management tests
3. Conflict resolution tests

---

## Estimated Test Counts

| Category | Test Files | Test Functions | Priority |
|----------|------------|----------------|----------|
| Logical Expressions | 1 | 30-40 | P0 |
| Sync Status | 1 | 20-25 | P0 |
| RepoMeta | 1 | 25-30 | P1 |
| RepoyardMeta | 1 | 10-15 | P1 |
| SyncRecord | 1 | 10-15 | P1 |
| Config Loading | 1 | 15-20 | P1 |
| Config Validation | 1 | 15-20 | P1 |
| Storage/Group Config | 2 | 15-20 | P2 |
| Base Utilities | 1 | 15-20 | P2 |
| rclone Commands | 1 | 10-15 | P2 |
| Sync Helper | 1 | 15-20 | P0 |
| Commands | 2 | 15-20 | P2 |
| **Unit Total** | ~14 | ~180-240 | |
| Integration (existing) | 3 | ~15 | P1 |
| Integration (new) | 4 | ~30-40 | P2 |
| **Integration Total** | ~7 | ~45-55 | |
| **Grand Total** | ~21 | ~225-295 | |

---

## Success Criteria

1. **Unit test coverage**: 80%+ for core modules
2. **All P0 tests passing**: Critical logic fully tested
3. **CI integration**: All unit tests run on every PR
4. **No regressions**: Existing integration tests still pass
5. **Documentation**: Each test file has clear docstrings explaining what it tests

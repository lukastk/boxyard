# ---
# jupyter:
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Unit Tests for Symlink Helpers
#
# Tests for get_repo_group_configs and symlink title generation logic.

# %%
#|default_exp unit.models.test_symlink_helpers

# %%
#|export
import pytest
from unittest.mock import MagicMock
from pathlib import Path

from repoyard._models import RepoMeta, get_repo_group_configs
from repoyard.config import RepoGroupConfig, VirtualRepoGroupConfig, RepoGroupTitleMode


# ============================================================================
# Fixtures
# ============================================================================

# %%
#|export
@pytest.fixture
def mock_config():
    """Create a mock config with repo groups and virtual groups."""
    config = MagicMock()
    config.repo_groups = {
        "backend": RepoGroupConfig(symlink_name="backend-projects"),
        "frontend": RepoGroupConfig(repo_title_mode=RepoGroupTitleMode.NAME),
    }
    config.virtual_repo_groups = {
        "active": VirtualRepoGroupConfig(filter_expr="NOT archived"),
    }
    return config


@pytest.fixture
def sample_repo_metas():
    """Create sample RepoMeta instances for testing."""
    return [
        RepoMeta(
            creation_timestamp_utc="20251120_100000",
            repo_subid="abc12",
            name="project-alpha",
            storage_location="default",
            creator_hostname="host1",
            groups=["backend", "python"],
        ),
        RepoMeta(
            creation_timestamp_utc="20251121_110000",
            repo_subid="def34",
            name="project-beta",
            storage_location="default",
            creator_hostname="host1",
            groups=["frontend", "custom-group"],
        ),
    ]


# ============================================================================
# Tests for get_repo_group_configs
# ============================================================================

# %%
#|export
class TestGetRepoGroupConfigs:
    """Tests for the get_repo_group_configs function."""

    def test_returns_config_groups(self, mock_config, sample_repo_metas):
        """Config groups are included in result."""
        groups, virtual_groups = get_repo_group_configs(mock_config, sample_repo_metas)

        assert "backend" in groups
        assert "frontend" in groups

    def test_returns_virtual_groups(self, mock_config, sample_repo_metas):
        """Virtual groups are returned separately."""
        groups, virtual_groups = get_repo_group_configs(mock_config, sample_repo_metas)

        assert "active" in virtual_groups
        assert isinstance(virtual_groups["active"], VirtualRepoGroupConfig)

    def test_adds_groups_from_repo_metas(self, mock_config, sample_repo_metas):
        """Groups from repo metas that aren't in config are added."""
        groups, virtual_groups = get_repo_group_configs(mock_config, sample_repo_metas)

        # "python" and "custom-group" are not in config but are in repo metas
        assert "python" in groups
        assert "custom-group" in groups

    def test_new_groups_have_default_config(self, mock_config, sample_repo_metas):
        """Groups not in config get default RepoGroupConfig."""
        groups, virtual_groups = get_repo_group_configs(mock_config, sample_repo_metas)

        # "python" should have default config
        assert isinstance(groups["python"], RepoGroupConfig)
        assert groups["python"].symlink_name is None
        assert groups["python"].repo_title_mode == RepoGroupTitleMode.INDEX_NAME

    def test_preserves_config_group_settings(self, mock_config, sample_repo_metas):
        """Groups from config preserve their settings."""
        groups, virtual_groups = get_repo_group_configs(mock_config, sample_repo_metas)

        # "backend" should keep its custom symlink_name
        assert groups["backend"].symlink_name == "backend-projects"

        # "frontend" should keep its custom title mode
        assert groups["frontend"].repo_title_mode == RepoGroupTitleMode.NAME

    def test_does_not_modify_original_config(self, mock_config, sample_repo_metas):
        """Original config.repo_groups is not modified."""
        original_keys = set(mock_config.repo_groups.keys())

        get_repo_group_configs(mock_config, sample_repo_metas)

        # Original config should not have new groups
        assert set(mock_config.repo_groups.keys()) == original_keys

    def test_empty_repo_metas(self, mock_config):
        """Works with empty repo metas list."""
        groups, virtual_groups = get_repo_group_configs(mock_config, [])

        # Should still have config groups
        assert "backend" in groups
        assert "frontend" in groups
        # But no groups from repos
        assert "python" not in groups
        assert "custom-group" not in groups

    def test_repo_with_no_groups(self, mock_config):
        """Handles repos with no groups."""
        repo = RepoMeta(
            creation_timestamp_utc="20251120_100000",
            repo_subid="xyz99",
            name="no-groups-repo",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )

        groups, virtual_groups = get_repo_group_configs(mock_config, [repo])

        # Should only have config groups
        assert set(groups.keys()) == {"backend", "frontend"}

    def test_empty_config_groups(self, sample_repo_metas):
        """Works when config has no predefined groups."""
        config = MagicMock()
        config.repo_groups = {}
        config.virtual_repo_groups = {}

        groups, virtual_groups = get_repo_group_configs(config, sample_repo_metas)

        # Should have groups from repo metas
        assert "backend" in groups
        assert "frontend" in groups
        assert "python" in groups
        assert "custom-group" in groups

    def test_duplicate_groups_in_repos(self, mock_config):
        """Same group in multiple repos is only added once."""
        repos = [
            RepoMeta(
                creation_timestamp_utc="20251120_100000",
                repo_subid="abc12",
                name="repo1",
                storage_location="default",
                creator_hostname="host",
                groups=["shared-group"],
            ),
            RepoMeta(
                creation_timestamp_utc="20251121_100000",
                repo_subid="def34",
                name="repo2",
                storage_location="default",
                creator_hostname="host",
                groups=["shared-group"],
            ),
        ]

        groups, virtual_groups = get_repo_group_configs(mock_config, repos)

        # "shared-group" should appear once
        assert "shared-group" in groups
        # Count occurrences (should be exactly 1)
        count = sum(1 for g in groups if g == "shared-group")
        assert count == 1


# ============================================================================
# Tests for symlink title generation logic
# ============================================================================

# %%
#|export
class TestSymlinkTitleGeneration:
    """Tests for symlink title generation based on RepoGroupTitleMode.

    These tests verify the title generation logic that would be used
    in create_user_repo_group_symlinks.
    """

    @pytest.fixture
    def repo_meta(self):
        """Create a sample RepoMeta for title testing."""
        return RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="a7kx9",
            name="my-project",
            storage_location="default",
            creator_hostname="host",
            groups=["backend"],
        )

    def test_title_mode_index_name(self, repo_meta):
        """INDEX_NAME mode uses the full index_name."""
        config = RepoGroupConfig(repo_title_mode=RepoGroupTitleMode.INDEX_NAME)

        # Simulate title generation logic
        if config.repo_title_mode == RepoGroupTitleMode.INDEX_NAME:
            title = repo_meta.index_name
        elif config.repo_title_mode == RepoGroupTitleMode.DATETIME_AND_NAME:
            title = f"{repo_meta.creation_timestamp_utc}__{repo_meta.name}"
        elif config.repo_title_mode == RepoGroupTitleMode.NAME:
            title = repo_meta.name

        assert title == "20251122_143022_a7kx9__my-project"

    def test_title_mode_datetime_and_name(self, repo_meta):
        """DATETIME_AND_NAME mode uses timestamp and name."""
        config = RepoGroupConfig(repo_title_mode=RepoGroupTitleMode.DATETIME_AND_NAME)

        if config.repo_title_mode == RepoGroupTitleMode.INDEX_NAME:
            title = repo_meta.index_name
        elif config.repo_title_mode == RepoGroupTitleMode.DATETIME_AND_NAME:
            title = f"{repo_meta.creation_timestamp_utc}__{repo_meta.name}"
        elif config.repo_title_mode == RepoGroupTitleMode.NAME:
            title = repo_meta.name

        assert title == "20251122_143022__my-project"

    def test_title_mode_name_only(self, repo_meta):
        """NAME mode uses just the repo name."""
        config = RepoGroupConfig(repo_title_mode=RepoGroupTitleMode.NAME)

        if config.repo_title_mode == RepoGroupTitleMode.INDEX_NAME:
            title = repo_meta.index_name
        elif config.repo_title_mode == RepoGroupTitleMode.DATETIME_AND_NAME:
            title = f"{repo_meta.creation_timestamp_utc}__{repo_meta.name}"
        elif config.repo_title_mode == RepoGroupTitleMode.NAME:
            title = repo_meta.name

        assert title == "my-project"

    def test_title_with_date_only_timestamp(self):
        """Title generation works with date-only timestamp."""
        repo = RepoMeta(
            creation_timestamp_utc="20251122",
            repo_subid="b8ly0",
            name="date-only-repo",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )

        config = RepoGroupConfig(repo_title_mode=RepoGroupTitleMode.DATETIME_AND_NAME)

        if config.repo_title_mode == RepoGroupTitleMode.DATETIME_AND_NAME:
            title = f"{repo.creation_timestamp_utc}__{repo.name}"

        assert title == "20251122__date-only-repo"

    def test_title_with_special_characters_in_name(self):
        """Title generation handles special characters in repo name."""
        repo = RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="c9mz1",
            name="my-project_v2.0",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )

        config = RepoGroupConfig(repo_title_mode=RepoGroupTitleMode.NAME)

        if config.repo_title_mode == RepoGroupTitleMode.NAME:
            title = repo.name

        assert title == "my-project_v2.0"


# ============================================================================
# Tests for symlink name resolution
# ============================================================================

# %%
#|export
class TestSymlinkNameResolution:
    """Tests for resolving symlink folder names from group configs."""

    def test_custom_symlink_name(self):
        """Custom symlink_name is used when specified."""
        config = RepoGroupConfig(symlink_name="custom-folder")

        symlink_name = config.symlink_name or "default-group"

        assert symlink_name == "custom-folder"

    def test_none_symlink_name_uses_group_name(self):
        """None symlink_name falls back to group name."""
        config = RepoGroupConfig(symlink_name=None)
        group_name = "backend"

        symlink_name = config.symlink_name or group_name

        assert symlink_name == "backend"

    def test_empty_string_symlink_name(self):
        """Empty string symlink_name is used as-is (truthy check)."""
        config = RepoGroupConfig(symlink_name="")
        group_name = "backend"

        # Empty string is falsy, so it falls back
        symlink_name = config.symlink_name or group_name

        assert symlink_name == "backend"

    def test_virtual_group_symlink_name(self):
        """Virtual groups also support custom symlink names."""
        config = VirtualRepoGroupConfig(
            filter_expr="backend",
            symlink_name="all-backend-projects",
        )

        symlink_name = config.symlink_name or "virtual-backend"

        assert symlink_name == "all-backend-projects"


# ============================================================================
# Tests for group membership checks
# ============================================================================

# %%
#|export
class TestGroupMembershipChecks:
    """Tests for checking if repos belong to groups."""

    def test_repo_in_regular_group(self):
        """Repo with group tag is in that group."""
        repo = RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="abc12",
            name="test-repo",
            storage_location="default",
            creator_hostname="host",
            groups=["backend", "python"],
        )

        assert "backend" in repo.groups
        assert "python" in repo.groups
        assert "frontend" not in repo.groups

    def test_repo_in_virtual_group_with_match(self):
        """Repo matching virtual group filter is in that group."""
        repo = RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="abc12",
            name="test-repo",
            storage_location="default",
            creator_hostname="host",
            groups=["backend", "active"],
        )

        virtual_config = VirtualRepoGroupConfig(filter_expr="backend AND active")

        assert virtual_config.is_in_group(repo.groups) is True

    def test_repo_not_in_virtual_group_without_match(self):
        """Repo not matching virtual group filter is not in that group."""
        repo = RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="abc12",
            name="test-repo",
            storage_location="default",
            creator_hostname="host",
            groups=["backend"],
        )

        virtual_config = VirtualRepoGroupConfig(filter_expr="backend AND frontend")

        assert virtual_config.is_in_group(repo.groups) is False

    def test_repo_excluded_by_not_filter(self):
        """Repo is excluded by NOT filter in virtual group."""
        repo = RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="abc12",
            name="test-repo",
            storage_location="default",
            creator_hostname="host",
            groups=["backend", "archived"],
        )

        virtual_config = VirtualRepoGroupConfig(filter_expr="backend AND NOT archived")

        assert virtual_config.is_in_group(repo.groups) is False

    def test_repo_with_empty_groups(self):
        """Repo with no groups is not in any regular group."""
        repo = RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="abc12",
            name="test-repo",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )

        assert "backend" not in repo.groups
        assert len(repo.groups) == 0

    def test_repo_with_empty_groups_matches_not_filter(self):
        """Repo with no groups can match NOT filters."""
        repo = RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="abc12",
            name="test-repo",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )

        virtual_config = VirtualRepoGroupConfig(filter_expr="NOT archived")

        # Empty groups means "archived" is not present, so NOT archived is True
        assert virtual_config.is_in_group(repo.groups) is True


# ============================================================================
# Tests for conflict handling (same title for different repos)
# ============================================================================

# %%
#|export
class TestSymlinkConflictHandling:
    """Tests for handling title conflicts when multiple repos have same title."""

    def test_same_name_different_timestamps_index_name_mode(self):
        """With INDEX_NAME mode, same-name repos have different titles."""
        repo1 = RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="abc12",
            name="my-project",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )
        repo2 = RepoMeta(
            creation_timestamp_utc="20251123_143022",
            repo_subid="def34",
            name="my-project",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )

        config = RepoGroupConfig(repo_title_mode=RepoGroupTitleMode.INDEX_NAME)

        title1 = repo1.index_name
        title2 = repo2.index_name

        # Titles should be different due to different timestamps and subids
        assert title1 != title2
        assert title1 == "20251122_143022_abc12__my-project"
        assert title2 == "20251123_143022_def34__my-project"

    def test_same_name_name_mode_potential_conflict(self):
        """With NAME mode, same-name repos would have the same title."""
        repo1 = RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="abc12",
            name="my-project",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )
        repo2 = RepoMeta(
            creation_timestamp_utc="20251123_143022",
            repo_subid="def34",
            name="my-project",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )

        config = RepoGroupConfig(repo_title_mode=RepoGroupTitleMode.NAME)

        title1 = repo1.name
        title2 = repo2.name

        # Titles would be the same - this is a conflict scenario
        assert title1 == title2 == "my-project"

    def test_datetime_and_name_same_name_different_times(self):
        """With DATETIME_AND_NAME mode, different timestamps prevent conflicts."""
        repo1 = RepoMeta(
            creation_timestamp_utc="20251122_143022",
            repo_subid="abc12",
            name="my-project",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )
        repo2 = RepoMeta(
            creation_timestamp_utc="20251123_143022",
            repo_subid="def34",
            name="my-project",
            storage_location="default",
            creator_hostname="host",
            groups=[],
        )

        config = RepoGroupConfig(repo_title_mode=RepoGroupTitleMode.DATETIME_AND_NAME)

        title1 = f"{repo1.creation_timestamp_utc}__{repo1.name}"
        title2 = f"{repo2.creation_timestamp_utc}__{repo2.name}"

        # Titles should be different due to different timestamps
        assert title1 != title2
        assert title1 == "20251122_143022__my-project"
        assert title2 == "20251123_143022__my-project"

#!/usr/bin/env python3
"""
Migration script: repoyard → boxyard

Migrates an existing repoyard installation to boxyard by:
1. Renaming configuration directories and files
2. Renaming data directories
3. Updating config.toml field names
4. Renaming repometa.toml → boxmeta.toml inside the local store
5. Renaming repoyard_meta.json → boxyard_meta.json

Usage:
    python repoyard_migrate.py              # Run migration with defaults
    python repoyard_migrate.py --dry-run    # Preview changes without applying
    python repoyard_migrate.py --config-path /path/to/config.toml  # Custom config location
"""

import argparse
import re
import shutil
import sys
from pathlib import Path


def log(msg: str, dry_run: bool = False):
    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}{msg}")


def rename_path(src: Path, dst: Path, dry_run: bool = False) -> bool:
    """Rename src to dst. Returns True if action was taken."""
    if not src.exists():
        return False
    if dst.exists():
        log(f"  SKIP (destination exists): {src} → {dst}", dry_run)
        return False
    log(f"  RENAME: {src} → {dst}", dry_run)
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    return True


def update_config_toml(config_path: Path, dry_run: bool = False):
    """Update field names in config.toml from repoyard to boxyard."""
    if not config_path.exists():
        log(f"  SKIP (not found): {config_path}", dry_run)
        return

    content = config_path.read_text()
    original = content

    # Field name replacements (order matters - longest first)
    replacements = [
        ("repoyard_data_path", "boxyard_data_path"),
        ("user_repo_groups_path", "user_box_groups_path"),
        ("user_repos_path", "user_boxes_path"),
        ("repo_timestamp_format", "box_timestamp_format"),
        ("default_repo_groups", "default_box_groups"),
        ("repo_subid_character_set", "box_subid_character_set"),
        ("repo_subid_length", "box_subid_length"),
        ("sync_before_new_repo", "sync_before_new_box"),
        ("[repo_groups]", "[box_groups]"),
        ("[virtual_repo_groups]", "[virtual_box_groups]"),
        ("repo_title_mode", "box_title_mode"),
        ("unique_repo_names", "unique_box_names"),
    ]

    for old, new in replacements:
        content = content.replace(old, new)

    if content != original:
        log(f"  UPDATE: {config_path}", dry_run)
        if not dry_run:
            config_path.write_text(content)
    else:
        log(f"  SKIP (no changes needed): {config_path}", dry_run)


def rename_repometas_in_store(local_store_path: Path, dry_run: bool = False):
    """Rename all repometa.toml → boxmeta.toml inside the local store."""
    if not local_store_path.exists():
        return
    count = 0
    for repometa in local_store_path.rglob("repometa.toml"):
        boxmeta = repometa.parent / "boxmeta.toml"
        if rename_path(repometa, boxmeta, dry_run):
            count += 1
    if count > 0:
        log(f"  Renamed {count} repometa.toml → boxmeta.toml files", dry_run)


def migrate(config_path: Path | None = None, dry_run: bool = False):
    """Run the full migration."""
    home = Path.home()

    # Determine config path
    old_config_dir = home / ".config" / "repoyard"
    new_config_dir = home / ".config" / "boxyard"

    if config_path is None:
        config_path = old_config_dir / "config.toml"
        if not config_path.exists():
            config_path = new_config_dir / "config.toml"

    print("=" * 60)
    print("Repoyard → Boxyard Migration")
    print("=" * 60)
    if dry_run:
        print("(DRY RUN - no changes will be made)\n")

    # Step 1: Rename config directory
    print("\n1. Configuration directory:")
    rename_path(old_config_dir, new_config_dir, dry_run)

    # After renaming, config_path might have changed
    if config_path.is_relative_to(old_config_dir):
        config_path = new_config_dir / config_path.relative_to(old_config_dir)

    # Step 2: Rename rclone config file
    print("\n2. Rclone config file:")
    config_dir = config_path.parent
    rename_path(
        config_dir / "repoyard_rclone.conf",
        config_dir / "boxyard_rclone.conf",
        dry_run,
    )

    # Step 3: Rename data directory
    print("\n3. Data directory:")
    old_data_dir = home / ".repoyard"
    new_data_dir = home / ".boxyard"
    rename_path(old_data_dir, new_data_dir, dry_run)

    # Step 4: Rename repoyard_meta.json → boxyard_meta.json
    print("\n4. Metadata file:")
    data_dir = new_data_dir if new_data_dir.exists() or not old_data_dir.exists() else old_data_dir
    rename_path(
        data_dir / "repoyard_meta.json",
        data_dir / "boxyard_meta.json",
        dry_run,
    )

    # Step 5: Rename repometa.toml files in local store
    print("\n5. Box metadata files (repometa.toml → boxmeta.toml):")
    local_store = data_dir / "local_store"
    rename_repometas_in_store(local_store, dry_run)

    # Step 6: Rename user directories
    print("\n6. User directories:")
    rename_path(home / "repos", home / "boxes", dry_run)
    rename_path(home / "repo-groups", home / "box-groups", dry_run)

    # Step 7: Update config.toml field names
    print("\n7. Config file field names:")
    actual_config = config_path if not dry_run else (
        config_path if config_path.exists() else
        old_config_dir / "config.toml"
    )
    update_config_toml(actual_config, dry_run)

    # Reminders
    print("\n" + "=" * 60)
    print("Post-migration reminders:")
    print("=" * 60)
    print(f"  - If you use REPOYARD_CONFIG_PATH env var, update it to BOXYARD_CONFIG_PATH")
    print(f"  - Update any shell aliases from 'repoyard' to 'boxyard'")
    print(f"  - The CLI command is now 'boxyard' instead of 'repoyard'")
    if dry_run:
        print(f"\nRun without --dry-run to apply these changes.")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate repoyard installation to boxyard"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=None,
        help="Path to config.toml (default: ~/.config/repoyard/config.toml)",
    )
    args = parser.parse_args()
    migrate(config_path=args.config_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

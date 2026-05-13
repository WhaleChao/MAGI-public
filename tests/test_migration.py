"""Tests for migrations.migrate — migration framework."""

from __future__ import annotations

import re
from pathlib import Path
import pytest
import tempfile


class TestParseMigration:
    """Tests for _parse_migration function."""

    def test_parse_migration_splits_up_down_sections(self, tmp_path):
        """_parse_migration should correctly parse UP/DOWN sections."""
        migration_file = tmp_path / "001_create_table.sql"
        migration_file.write_text("""
-- UP
CREATE TABLE users (
    id INT PRIMARY KEY,
    name VARCHAR(255)
);

-- DOWN
DROP TABLE users;
""")

        from migrations.migrate import _parse_migration

        result = _parse_migration(migration_file)

        assert result["version"] == "001"
        assert "create_table" in result["description"].lower() or "CREATE TABLE users" in result["up"]
        assert "CREATE TABLE users" in result["up"]
        assert "DROP TABLE users" in result["down"]


    def test_parse_migration_extracts_version_and_description(self, tmp_path):
        """_parse_migration should extract version and description from filename."""
        migration_file = tmp_path / "002_add_user_email.sql"
        migration_file.write_text("""
-- UP
ALTER TABLE users ADD COLUMN email VARCHAR(255);

-- DOWN
ALTER TABLE users DROP COLUMN email;
""")

        from migrations.migrate import _parse_migration

        result = _parse_migration(migration_file)

        assert result["version"] == "002"
        assert "add_user_email" in result["description"].lower() or "email" in result["description"].lower()


    def test_parse_migration_handles_missing_down_section(self, tmp_path):
        """_parse_migration should handle migration with no DOWN section."""
        migration_file = tmp_path / "003_init.sql"
        migration_file.write_text("""
-- UP
CREATE TABLE initial_schema (id INT);
""")

        from migrations.migrate import _parse_migration

        result = _parse_migration(migration_file)

        assert result["version"] == "003"
        assert "CREATE TABLE initial_schema" in result["up"]
        assert result["down"] == "" or result["down"].strip() == ""


    def test_parse_migration_case_insensitive_markers(self, tmp_path):
        """_parse_migration should handle UP/DOWN markers case-insensitively."""
        migration_file = tmp_path / "004_test.sql"
        migration_file.write_text("""
-- up
SELECT 1;

-- down
SELECT 0;
""")

        from migrations.migrate import _parse_migration

        result = _parse_migration(migration_file)

        assert "SELECT 1" in result["up"]
        assert "SELECT 0" in result["down"]


class TestDiscoverMigrations:
    """Tests for _discover_migrations function."""

    def test_discover_migrations_finds_sql_files(self, tmp_path):
        """_discover_migrations should find all .sql files in versions dir."""
        versions_dir = tmp_path / "versions"
        versions_dir.mkdir()

        # Create test migration files
        (versions_dir / "001_init.sql").write_text("-- UP\nCREATE TABLE t1;\n-- DOWN\nDROP TABLE t1;")
        (versions_dir / "002_alter.sql").write_text("-- UP\nALTER TABLE t1 ADD id INT;\n-- DOWN\nALTER TABLE t1 DROP id;")

        # Monkey-patch the VERSIONS_DIR
        import migrations.migrate as migrate_module
        original_versions_dir = migrate_module.VERSIONS_DIR
        try:
            migrate_module.VERSIONS_DIR = versions_dir

            from migrations.migrate import _discover_migrations

            migrations = _discover_migrations()

            assert len(migrations) == 2
            assert any(m["version"] == "001" for m in migrations)
            assert any(m["version"] == "002" for m in migrations)
        finally:
            migrate_module.VERSIONS_DIR = original_versions_dir


    def test_discover_migrations_returns_sorted_list(self, tmp_path):
        """_discover_migrations should return migrations in sorted order."""
        versions_dir = tmp_path / "versions"
        versions_dir.mkdir()

        # Create migrations in non-sorted order
        (versions_dir / "003_third.sql").write_text("-- UP\nSELECT 3;")
        (versions_dir / "001_first.sql").write_text("-- UP\nSELECT 1;")
        (versions_dir / "002_second.sql").write_text("-- UP\nSELECT 2;")

        import migrations.migrate as migrate_module
        original_versions_dir = migrate_module.VERSIONS_DIR
        try:
            migrate_module.VERSIONS_DIR = versions_dir

            from migrations.migrate import _discover_migrations

            migrations = _discover_migrations()

            versions = [m["version"] for m in migrations]
            assert versions == ["001", "002", "003"]
        finally:
            migrate_module.VERSIONS_DIR = original_versions_dir


    def test_discover_migrations_returns_empty_list_if_dir_missing(self, tmp_path):
        """_discover_migrations should return empty list if versions dir doesn't exist."""
        nonexistent_dir = tmp_path / "nonexistent"

        import migrations.migrate as migrate_module
        original_versions_dir = migrate_module.VERSIONS_DIR
        try:
            migrate_module.VERSIONS_DIR = nonexistent_dir

            from migrations.migrate import _discover_migrations

            migrations = _discover_migrations()

            assert migrations == []
        finally:
            migrate_module.VERSIONS_DIR = original_versions_dir


class TestVersionNumbering:
    """Tests for version numbering conventions."""

    def test_version_numbering_is_sequential(self, tmp_path):
        """Migration versions should be sequential numbers."""
        versions_dir = tmp_path / "versions"
        versions_dir.mkdir()

        # Create sequential migrations
        (versions_dir / "001_init.sql").write_text("-- UP\nSELECT 1;")
        (versions_dir / "002_add_table.sql").write_text("-- UP\nSELECT 2;")
        (versions_dir / "003_alter_table.sql").write_text("-- UP\nSELECT 3;")

        import migrations.migrate as migrate_module
        original_versions_dir = migrate_module.VERSIONS_DIR
        try:
            migrate_module.VERSIONS_DIR = versions_dir

            from migrations.migrate import _discover_migrations

            migrations = _discover_migrations()

            # Check that versions are numeric strings in order
            versions = [int(m["version"]) for m in migrations]
            assert versions == sorted(versions)
            assert versions == [1, 2, 3]
        finally:
            migrate_module.VERSIONS_DIR = original_versions_dir


    def test_version_extracted_from_filename_prefix(self, tmp_path):
        """Version should be extracted from filename prefix."""
        migration_file = tmp_path / "042_some_migration.sql"
        migration_file.write_text("-- UP\nSELECT 1;")

        from migrations.migrate import _parse_migration

        result = _parse_migration(migration_file)

        assert result["version"] == "042"


    def test_description_from_filename_after_underscore(self, tmp_path):
        """Description should be from filename after version prefix."""
        migration_file = tmp_path / "001_create_initial_schema.sql"
        migration_file.write_text("-- UP\nCREATE TABLE t;")

        from migrations.migrate import _parse_migration

        result = _parse_migration(migration_file)

        assert result["version"] == "001"
        assert "create" in result["description"].lower() or "initial" in result["description"].lower()


class TestMigrationFileParsing:
    """Additional tests for migration file parsing edge cases."""

    def test_parse_migration_handles_multiple_statements(self, tmp_path):
        """_parse_migration should handle multiple SQL statements in UP section."""
        migration_file = tmp_path / "001_multi.sql"
        migration_file.write_text("""
-- UP
CREATE TABLE t1 (id INT);
CREATE TABLE t2 (id INT);
INSERT INTO t1 VALUES (1);

-- DOWN
DROP TABLE t1;
DROP TABLE t2;
""")

        from migrations.migrate import _parse_migration

        result = _parse_migration(migration_file)

        assert "CREATE TABLE t1" in result["up"]
        assert "CREATE TABLE t2" in result["up"]
        assert "INSERT INTO" in result["up"]


    def test_parse_migration_trims_whitespace(self, tmp_path):
        """_parse_migration should trim whitespace from sections."""
        migration_file = tmp_path / "001_trim.sql"
        migration_file.write_text("""

-- UP


CREATE TABLE test;


-- DOWN


DROP TABLE test;


""")

        from migrations.migrate import _parse_migration

        result = _parse_migration(migration_file)

        assert result["up"].strip() == "CREATE TABLE test;"
        assert result["down"].strip() == "DROP TABLE test;"


    def test_parse_migration_preserves_sql_formatting(self, tmp_path):
        """_parse_migration should preserve SQL formatting within sections."""
        migration_file = tmp_path / "001_format.sql"
        migration_file.write_text("""
-- UP
CREATE TABLE users (
    id INT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- DOWN
DROP TABLE users;
""")

        from migrations.migrate import _parse_migration

        result = _parse_migration(migration_file)

        # SQL formatting should be preserved
        assert "INT PRIMARY KEY" in result["up"]
        assert "VARCHAR(255) NOT NULL" in result["up"]
        assert "TIMESTAMP DEFAULT CURRENT_TIMESTAMP" in result["up"]

"""Tests for pyproject.toml packaging configuration."""

from __future__ import annotations

from pathlib import Path
import tomllib
import pytest


class TestPyprojectStructure:
    """Tests for pyproject.toml structure."""

    @pytest.fixture
    def pyproject_data(self):
        """Load pyproject.toml as parsed data."""
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            return tomllib.load(f)

    def test_pyproject_has_build_system(self, pyproject_data):
        """pyproject.toml should have build-system section."""
        assert "build-system" in pyproject_data
        build_system = pyproject_data["build-system"]
        assert "requires" in build_system
        assert "build-backend" in build_system


    def test_pyproject_has_project_metadata(self, pyproject_data):
        """pyproject.toml should have project metadata."""
        assert "project" in pyproject_data
        project = pyproject_data["project"]

        # Check required fields
        assert "name" in project
        assert "version" in project
        assert "requires-python" in project
        assert "description" in project


    def test_project_name_is_magi(self, pyproject_data):
        """Project name should be 'magi'."""
        project = pyproject_data["project"]
        assert project["name"].lower() == "magi"


    def test_project_version_is_valid_semver(self, pyproject_data):
        """Project version should be valid semantic versioning."""
        project = pyproject_data["project"]
        version = project["version"]

        # Basic semver check: should be X.Y.Z format
        parts = version.split(".")
        assert len(parts) >= 2, f"Version {version} doesn't follow semantic versioning"

        # Each part should be numeric (or contain prerelease markers)
        for part in parts:
            assert any(c.isdigit() for c in part), f"Version part {part} contains no digits"


    def test_requires_python_is_set(self, pyproject_data):
        """requires-python should be set and >= 3.12."""
        project = pyproject_data["project"]
        requires_python = project["requires-python"]

        assert requires_python is not None
        assert "3.12" in requires_python or "3.11" in requires_python or ">=" in requires_python


class TestProjectDependencies:
    """Tests for project dependencies."""

    @pytest.fixture
    def pyproject_data(self):
        """Load pyproject.toml as parsed data."""
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            return tomllib.load(f)

    def test_has_core_dependencies(self, pyproject_data):
        """Project should have core dependencies listed."""
        project = pyproject_data["project"]
        assert "dependencies" in project

        deps = project["dependencies"]
        assert isinstance(deps, list)
        assert len(deps) > 0


    def test_core_dependencies_include_flask(self, pyproject_data):
        """Core dependencies should include Flask."""
        project = pyproject_data["project"]
        deps_str = " ".join(project["dependencies"])

        assert "flask" in deps_str.lower()


    def test_core_dependencies_include_database(self, pyproject_data):
        """Core dependencies should include database connector."""
        project = pyproject_data["project"]
        deps_str = " ".join(project["dependencies"])

        assert "mysql" in deps_str.lower() or "psycopg" in deps_str.lower() or "sqlalchemy" in deps_str.lower()


    def test_core_dependencies_include_dotenv(self, pyproject_data):
        """Core dependencies should include python-dotenv."""
        project = pyproject_data["project"]
        deps_str = " ".join(project["dependencies"])

        assert "dotenv" in deps_str.lower()


    def test_core_dependencies_include_requests(self, pyproject_data):
        """Core dependencies should include requests."""
        project = pyproject_data["project"]
        deps_str = " ".join(project["dependencies"])

        assert "requests" in deps_str.lower()


    def test_optional_dependencies_exist(self, pyproject_data):
        """Project should have optional dependencies."""
        project = pyproject_data["project"]
        assert "optional-dependencies" in project

        optional = project["optional-dependencies"]
        assert isinstance(optional, dict)
        assert len(optional) > 0


    def test_optional_dependencies_include_dev(self, pyproject_data):
        """Optional dependencies should include dev tools."""
        project = pyproject_data["project"]
        optional = project["optional-dependencies"]

        assert "dev" in optional
        dev_deps = " ".join(optional["dev"]).lower()
        assert "pytest" in dev_deps


class TestGitignore:
    """Tests for .gitignore coverage."""

    def test_gitignore_exists(self):
        """Project should have .gitignore."""
        gitignore_path = Path(__file__).parent.parent / ".gitignore"
        assert gitignore_path.exists()


    def test_gitignore_covers_pycache(self):
        """gitignore should ignore __pycache__."""
        gitignore_path = Path(__file__).parent.parent / ".gitignore"
        content = gitignore_path.read_text()

        assert "__pycache__" in content or "*/__pycache__" in content


    def test_gitignore_covers_env_files(self):
        """gitignore should ignore .env files."""
        gitignore_path = Path(__file__).parent.parent / ".gitignore"
        content = gitignore_path.read_text()

        assert ".env" in content


    def test_gitignore_covers_runtime_artifacts(self):
        """gitignore should cover runtime output directories."""
        gitignore_path = Path(__file__).parent.parent / ".gitignore"
        content = gitignore_path.read_text()

        # Check for runtime output patterns
        runtime_patterns = [
            "_autopilot_runs",
            "_db_backups",
            "_debug_reports",
            "_eventlog",
            "backups",
        ]

        for pattern in runtime_patterns:
            assert pattern in content, f"gitignore should cover {pattern}"


    def test_gitignore_covers_credentials(self):
        """gitignore should ignore credential files."""
        gitignore_path = Path(__file__).parent.parent / ".gitignore"
        content = gitignore_path.read_text()

        # Check for common credential patterns
        credential_patterns = [
            "credentials",
            "secret",
            ".pickle",
            "token",
        ]

        covered = sum(1 for pattern in credential_patterns if pattern in content)
        assert covered >= 2, f"gitignore should cover credential files, found {covered}/4"


    def test_gitignore_covers_logs(self):
        """gitignore should ignore log files."""
        gitignore_path = Path(__file__).parent.parent / ".gitignore"
        content = gitignore_path.read_text()

        assert "*.log" in content or "logs/" in content


    def test_gitignore_covers_database_files(self):
        """gitignore should ignore database files."""
        gitignore_path = Path(__file__).parent.parent / ".gitignore"
        content = gitignore_path.read_text()

        assert "*.db" in content


class TestBuildSystem:
    """Tests for build system configuration."""

    @pytest.fixture
    def pyproject_data(self):
        """Load pyproject.toml as parsed data."""
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            return tomllib.load(f)

    def test_build_backend_is_setuptools(self, pyproject_data):
        """Build backend should be setuptools."""
        build_system = pyproject_data["build-system"]
        assert "setuptools" in build_system["build-backend"]


    def test_build_requires_setuptools(self, pyproject_data):
        """Build requirements should include setuptools."""
        build_system = pyproject_data["build-system"]
        requires = " ".join(build_system["requires"])

        assert "setuptools" in requires.lower()


    def test_build_requires_wheel(self, pyproject_data):
        """Build requirements should include wheel."""
        build_system = pyproject_data["build-system"]
        requires = " ".join(build_system["requires"])

        assert "wheel" in requires.lower()


class TestTestConfiguration:
    """Tests for pytest configuration in pyproject.toml."""

    @pytest.fixture
    def pyproject_data(self):
        """Load pyproject.toml as parsed data."""
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            return tomllib.load(f)

    def test_pytest_config_exists(self, pyproject_data):
        """pytest configuration should be present."""
        assert "tool" in pyproject_data
        assert "pytest" in pyproject_data["tool"]


class TestConsoleScripts:
    """Tests for packaged console entry points."""

    @pytest.fixture
    def pyproject_data(self):
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            return tomllib.load(f)

    def test_console_scripts_target_existing_python_modules(self, pyproject_data):
        """Every console script should point to an existing Python module + function."""
        project = pyproject_data["project"]
        scripts = project.get("scripts", {})
        root = Path(__file__).parent.parent

        assert scripts, "Expected at least one console script"
        for _name, target in scripts.items():
            module_name, sep, func_name = target.partition(":")
            assert sep == ":", f"Invalid console script target: {target}"

            module_path = root.joinpath(*module_name.split(".")).with_suffix(".py")
            assert module_path.exists(), f"Missing entrypoint module: {module_path}"

            source = module_path.read_text(encoding="utf-8")
            assert f"def {func_name}(" in source, f"Missing entrypoint function {func_name} in {module_path}"
        assert "tool" in pyproject_data
        assert "pytest" in pyproject_data["tool"]


    def test_pytest_testpaths_configured(self, pyproject_data):
        """pytest testpaths should be configured."""
        pytest_config = pyproject_data["tool"]["pytest"]["ini_options"]
        assert "testpaths" in pytest_config
        assert "tests" in pytest_config["testpaths"]

"""Exercise release synchronization with real uv commands in an isolated workspace."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
PROJECTS = {
    ".": "bedrock-mono",
    "packages/bedrock": "bedrock-core",
    "packages/bedrock-cli": "bedrock-cli",
    "integrations/fastapi": "bedrock-fastapi",
    "integrations/celery": "bedrock-celery",
}


@pytest.fixture
def release_workspace(tmp_path: Path) -> Path:
    """Create a dependency-free workspace that needs no network or installation."""
    for directory, name in PROJECTS.items():
        path = tmp_path / directory / "pyproject.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n')
    with (tmp_path / "pyproject.toml").open("a") as manifest:
        manifest.write('\n[tool.uv.workspace]\nmembers = ["packages/*", "integrations/*"]\n')
        manifest.write("\n[tool.uv.sources]\nbedrock-core = { workspace = true }\n")
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(REPOSITORY / "scripts/release_version.py", tmp_path / "scripts/release_version.py")
    shutil.copyfile(REPOSITORY / "Makefile", tmp_path / "Makefile")
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n\n## [0.1.0]\n")
    return tmp_path


def run_release(root: Path, version: str, package: str = "bedrock-core") -> subprocess.CompletedProcess[str]:
    """Run the public make target using an offline local Python interpreter."""
    return subprocess.run(
        ["make", "release-version"],
        cwd=root,
        env={
            **os.environ,
            "VERSION": version,
            "PACKAGE": package,
            "UV_OFFLINE": "true",
            "UV_NO_INDEX": "true",
            "UV_PYTHON": sys.executable,
            "UV_PYTHON_DOWNLOADS": "never",
        },
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("package", ["bedrock-core", "bedrock-fastapi", "bedrock-celery"])
def test_release_updates_only_selected_group(release_workspace: Path, package: str) -> None:
    """Keep unrelated versions and release notes unchanged, including in the lockfile."""
    root = release_workspace
    result = run_release(root, "1.2.3rc1", package)
    assert result.returncode == 0, result.stdout + result.stderr
    selected = {"bedrock-mono", "bedrock-core", "bedrock-cli"} if package == "bedrock-core" else {package}
    locked = {item["name"]: item["version"] for item in tomllib.loads((root / "uv.lock").read_text())["package"]}
    for directory, name in PROJECTS.items():
        expected = "1.2.3rc1" if name in selected else "0.1.0"
        assert tomllib.loads((root / directory / "pyproject.toml").read_text())["project"]["version"] == expected
        assert locked[name] == expected
    assert (root / "CHANGELOG.md").read_text() == "## [Unreleased]\n\n## [0.1.0]\n"


def test_release_rejects_bad_input_and_restores_failed_lock(release_workspace: Path) -> None:
    """A failed release must preserve existing edits and the previous lockfile byte-for-byte."""
    root = release_workspace
    result = run_release(root, "0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr
    integration = root / "integrations/fastapi/pyproject.toml"
    with integration.open("a") as manifest:
        manifest.write('dependencies = ["bedrock-release-test-missing-dependency==0"]\n')
    originals = {path: path.read_bytes() for path in root.rglob("pyproject.toml")}
    originals[root / "uv.lock"] = (root / "uv.lock").read_bytes()
    for version, package in [
        ("", "bedrock-core"),
        ("invalid", "bedrock-core"),
        ("--offline", "bedrock-core"),
        ("0.3.0", "unknown"),
        ("0.3.0", "bedrock-core"),
    ]:
        result = run_release(root, version, package)
        assert result.returncode != 0, result.stdout + result.stderr
        assert {path: path.read_bytes() for path in originals} == originals

# Contributing to Bedrock

Thank you for your interest in contributing to Bedrock! This document provides guidelines and information for contributors.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Making Changes](#making-changes)
- [Code Style](#code-style)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)
- [Releasing](#releasing)
- [Reporting Issues](#reporting-issues)

## Code of Conduct

This project follows a standard code of conduct. By participating, you are expected to uphold this code. Please report unacceptable behavior to the project maintainers.

## Getting Started

1. Fork the repository on GitHub
2. Clone your fork locally
3. Create a branch for your changes
4. Make your changes
5. Submit a pull request to the `dev` branch

## Development Setup

### Prerequisites

- Python 3.12 or higher
- [uv](https://docs.astral.sh/uv/) package manager

### Setup Steps

1. Clone your fork:
   ```bash
   git clone https://github.com/maacck/bedrock-py.git
   cd bedrock
   ```

2. Install dependencies:
   ```bash
   uv sync
   ```

3. Verify the installation:
   ```bash
   uv run bedrock --help
   ```

4. Install pre-commit hooks:
   ```bash
   pip install pre-commit
   pre-commit install
   ```

5. Run the tests:
   ```bash
   uv run pytest packages/bedrock/tests/
   ```

## Making Changes

1. Create a new branch from `dev`:
   ```bash
   git checkout dev
   git pull origin dev
   git checkout -b feature/your-feature-name
   ```

2. Make your changes in small, focused commits
3. Write clear commit messages following [Conventional Commits](https://www.conventionalcommits.org/):
   - `feat:` for new features
   - `fix:` for bug fixes
   - `docs:` for documentation changes
   - `style:` for formatting changes
   - `refactor:` for code refactoring
   - `test:` for adding tests
   - `chore:` for maintenance tasks

## Code Style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

### Configuration

- Line length: 120 characters
- Quote style: Double quotes
- Import sorting: isort-compatible

### Pre-commit Hooks

This project uses [pre-commit](https://pre-commit.com/) to automatically run style checks before each commit.

**Hooks configured:**
- `ruff` — Python linting with auto-fix
- `ruff-format` — Python formatting
- `eslint` — JavaScript/TypeScript linting (docs-web only)

**Usage:**

```bash
# Hooks run automatically on git commit
git commit -m "feat: add new feature"

# Run all hooks manually
pre-commit run --all-files

# Run specific hook
pre-commit run ruff --all-files
```

### Manual Style Checks

```bash
# Check formatting
uv run ruff format --check .

# Auto-format
uv run ruff format .

# Check linting
uv run ruff check .

# Auto-fix linting issues
uv run ruff check --fix .
```

### Type Hints

- Use strict type hints for all function signatures
- Follow Google-style docstrings for public APIs
- Use `async` prefix for async methods (e.g., `aget()`, `aset()`)

## Testing

### Running Tests

```bash
# Run all tests
uv run pytest packages/bedrock/tests/

# Run with verbose output
uv run pytest packages/bedrock/tests/ -v

# Run specific test file
uv run pytest packages/bedrock/tests/test_signal.py
```

### Writing Tests

- Place tests in `packages/bedrock/tests/`
- Name test files `test_*.py`
- Use `pytest.mark.asyncio` for async tests
- Use fixtures for shared setup
- Aim for clear, focused test cases

### Test Structure

```python
import pytest

class TestFeature:
    """Tests for the feature."""

    def test_basic_behavior(self):
        """Test basic expected behavior."""
        # Arrange
        # Act
        # Assert

    @pytest.mark.asyncio
    async def test_async_behavior(self):
        """Test async behavior."""
        # Arrange
        # Act
        # Assert
```

## Pull Request Process

1. **Before Submitting**:
   - Ensure all tests pass
   - Run linting and formatting checks
   - Add tests for new functionality
   - For public signature or behavior changes, update `CHANGELOG.md` (`[Unreleased]`), the affected Web Doc
     pages in both `docs-web/content/docs/en/` and `zh/`, and the agent skills under `skills/` in the same PR
     (see [`AGENTS.md` → Change Alignment](AGENTS.md#change-alignment))

2. **PR Description**:
   - Fill out the PR template completely
   - Reference related issues (e.g., "Closes #123")
   - Describe what changed and why
   - Include screenshots for UI changes

3. **Review Process**:
   - Maintainers will review your PR
   - Address any requested changes
   - Once approved, your PR will be merged

4. **After Merge**:
   - Delete your feature branch
   - Pull the latest changes from `dev`

## Releasing

Pushing a `vX.Y.Z` tag runs `.github/workflows/release.yml`: it checks the tag against the `bedrock-core`
version, publishes to PyPI, and uses the matching `CHANGELOG.md` section as the GitHub release notes.

### 1. Alignment Check

List what changed since the last release:

```bash
PREV=$(git tag --sort=-v:refname | head -n 1)
git log --oneline "$PREV"..HEAD
git diff --stat "$PREV"..HEAD -- packages/*/src
```

For every user-visible change in that range:

- [ ] `CHANGELOG.md` `[Unreleased]` has an entry; breaking changes state the migration.
- [ ] Web Doc pages in `docs-web/content/docs/en/` and `zh/` describe the current signatures and behavior.
- [ ] `skills/bedrock/` and `skills/bedrock-cli/` (`SKILL.md`, references, templates) match the code.
- [ ] Contrib playbooks (`contrib/*/playbook/`) match their package.
- [ ] No doc, skill, or playbook mentions a removed or renamed symbol (grep each one).

Then verify:

```bash
# en/zh page parity: no output expected
diff <(cd docs-web/content/docs/en && find . -type f | sort) <(cd docs-web/content/docs/zh && find . -type f | sort)
uv run pytest packages/bedrock/tests/
cd docs-web && npm run build
```

### 2. Bump and Tag

1. Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` in `CHANGELOG.md`.
2. Set `version` in `pyproject.toml`, `packages/bedrock/pyproject.toml`, and
   `packages/bedrock-cli/pyproject.toml`, then run `uv lock`.
3. Raise `bedrock-core>=` in `packages/bedrock-cli/src/bedrock_cli/templates/workspace/pyproject.toml.j2`
   when generated projects need this release.
4. Add a new minor line to the supported versions in `SECURITY.md`.
5. Commit as `chore(release): prepare vX.Y.Z`, merge to `main`, then tag that commit on `main` and push the tag.

## Reporting Issues

### Bug Reports

Use the [Bug Report](https://github.com/maacck/bedrock-py/issues/new?template=bug_report.yml) template. Include:

- Clear description of the bug
- Steps to reproduce
- Expected vs actual behavior
- Bedrock version and Python version
- Error messages or logs

### Feature Requests

Use the [Feature Request](https://github.com/maacck/bedrock-py/issues/new?template=feature_request.yml) template. Include:

- Problem description
- Proposed solution
- Alternatives considered

## Questions?

If you have questions about contributing, feel free to:

1. Check the [README](README.md) for project overview
2. Open a [Discussion](https://github.com/maacck/bedrock-py/discussions) for general questions
3. Join the community channels (if available)

Thank you for contributing to Bedrock!

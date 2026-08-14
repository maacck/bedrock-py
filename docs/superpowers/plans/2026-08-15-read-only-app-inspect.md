# Read-only `bedrock app inspect` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make module and dependency inspection validate optional installation hooks without executing installation code.

**Architecture:** Centralize optional hook loading and signature validation in one private helper in `bedrock.cli.apps`. Both inspection flows use it; `bedrock app install` remains the sole lifecycle executor.

**Tech Stack:** Python 3.13, Typer, Rich, pytest, MDX.

## Global Constraints

- `installation.py`, `pre_install`, `install`, and `post_install` are optional for `inspect`.
- `inspect` must never invoke an installation hook for the requested module or any dependency.
- Present hooks must pass `_check_installation_hooks` and accept `**kwargs`.
- Invalid present hooks fail inspection; absent hook sets pass as optional.
- `bedrock app install` preserves `pre_install` → `install` → `post_install` execution order.
- Edit only English and Chinese documentation source under `docs-web/content/docs/`.
- Keep Python identifiers and docstrings in English; preserve the existing documentation locales.
- Do not commit until post-implementation review and explicit user approval.

---

## File structure

- Modify `packages/bedrock/src/bedrock/cli/apps.py`: add shared read-only hook validation; update both inspection paths and output/table status.
- Modify `packages/bedrock/tests/test_cli_helpers.py`: test zero execution for both inspection paths, optional absence, invalid signatures, and retained install order.
- Modify `docs-web/content/docs/en/(bedrock)/guides/cli.mdx` and `docs-web/content/docs/zh/(bedrock)/guides/cli.mdx`: explain validation-only inspect versus executing app install.

### Task 1: Test and implement read-only inspection

**Files:**
- Modify: `packages/bedrock/tests/test_cli_helpers.py:77-135`
- Modify: `packages/bedrock/src/bedrock/cli/apps.py:47-157, 209-217`

**Interfaces:**
- Consumes: `load_optional_callable(ref: str) -> Callable[..., Any] | None` and `_check_installation_hooks(func: Callable) -> None`.
- Produces: `_validate_installation_hooks(app_name: str) -> bool`; it returns `True` only when one or more optional hooks were found and every present hook has a valid signature. It never invokes a callable.

- [ ] **Step 1: Replace the side-effect test with a dependency no-execution test.**

```python
def test_inspect_dependency_validates_hooks_without_execution(self, monkeypatch: pytest.MonkeyPatch) -> None:
    hook_calls: list[str] = []
    validated_hooks: list[str] = []

    def install(**kwargs: object) -> None:
        del kwargs
        hook_calls.append("install")

    def pre_install(**kwargs: object) -> None:
        del kwargs
        hook_calls.append("pre_install")

    def post_install(**kwargs: object) -> None:
        del kwargs
        hook_calls.append("post_install")

    monkeypatch.setattr(cli_apps, "find_spec", lambda dep_path: object())
    monkeypatch.setattr(cli_apps, "load_manifest", lambda dep_path: object())
    monkeypatch.setattr(
        cli_apps,
        "build_app_config",
        lambda dep_path: SimpleNamespace(name=dep_path, bootstrap_module=object(), models_module=None),
    )
    monkeypatch.setattr(
        cli_apps,
        "load_optional_callable",
        lambda path: {
            "demo.app.installation:pre_install": pre_install,
            "demo.app.installation:install": install,
            "demo.app.installation:post_install": post_install,
        }.get(path),
    )
    monkeypatch.setattr(cli_apps, "_check_installation_hooks", lambda func: validated_hooks.append(func.__name__))

    result = cli_apps._inspect_dependency("demo.app")

    assert result.installation_valid is True
    assert result.errors == []
    assert validated_hooks == ["pre_install", "install", "post_install"]
    assert hook_calls == []
```

- [ ] **Step 2: Add the equivalent primary-module regression.**

```python
def test_basic_inspect_validates_hooks_without_execution(self, monkeypatch: pytest.MonkeyPatch) -> None:
    hook_calls: list[str] = []

    def install(**kwargs: object) -> None:
        del kwargs
        hook_calls.append("install")

    monkeypatch.setattr(cli_apps, "load_manifest", lambda import_path: object())
    monkeypatch.setattr(
        cli_apps,
        "build_app_config",
        lambda import_path: SimpleNamespace(name=import_path, bootstrap_module=None, models_module=None),
    )
    monkeypatch.setattr(
        cli_apps,
        "load_optional_callable",
        lambda path: install if path == "demo.app.installation:install" else None,
    )

    result = cli_apps._run_basic_inspect("demo.app", MagicMock())

    assert result.installation_valid is True
    assert result.errors == []
    assert hook_calls == []
```

- [ ] **Step 3: Add optional-absence and invalid-signature regression tests.**

```python
def test_inspect_dependency_allows_missing_installation_hooks(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_apps, "find_spec", lambda dep_path: object())
    monkeypatch.setattr(cli_apps, "load_manifest", lambda dep_path: object())
    monkeypatch.setattr(
        cli_apps,
        "build_app_config",
        lambda dep_path: SimpleNamespace(name=dep_path, bootstrap_module=None, models_module=None),
    )
    monkeypatch.setattr(cli_apps, "load_optional_callable", lambda path: None)

    result = cli_apps._inspect_dependency("demo.app")

    assert result.installation_valid is False
    assert result.errors == []
```

```python
def test_inspect_dependency_reports_invalid_present_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
    def install() -> None:
        return None

    monkeypatch.setattr(cli_apps, "find_spec", lambda dep_path: object())
    monkeypatch.setattr(cli_apps, "load_manifest", lambda dep_path: object())
    monkeypatch.setattr(
        cli_apps,
        "build_app_config",
        lambda dep_path: SimpleNamespace(name=dep_path, bootstrap_module=None, models_module=None),
    )
    monkeypatch.setattr(
        cli_apps,
        "load_optional_callable",
        lambda path: install if path == "demo.app.installation:install" else None,
    )

    result = cli_apps._inspect_dependency("demo.app")

    assert result.installation_valid is False
    assert "must accept **kwargs" in result.errors[0]
```

- [ ] **Step 4: Run the failing no-execution feedback loop.**

Run:

```bash
uv run pytest packages/bedrock/tests/test_cli_helpers.py::TestAppsHelpers::test_inspect_dependency_validates_hooks_without_execution -q
uv run pytest packages/bedrock/tests/test_cli_helpers.py::TestAppsHelpers::test_basic_inspect_validates_hooks_without_execution -q
```

Expected: both fail because current code invokes `installation()`.

- [ ] **Step 5: Implement the shared validator.**

Add after `_check_installation_hooks`:

```python
def _validate_installation_hooks(app_name: str) -> bool:
    """Validate optional installation-hook signatures without executing them."""
    hooks_found = False
    for hook_name in ("pre_install", "install", "post_install"):
        hook = load_optional_callable(f"{app_name}.installation:{hook_name}")
        if hook is None:
            continue
        hooks_found = True
        _check_installation_hooks(hook)
    return hooks_found
```

In `_run_basic_inspect` and `_inspect_dependency`, replace manual loading, missing-install errors, and `installation()` calls with the helper. Set `installation_valid` to its result. The primary flow prints `Installation hooks for '<module>' are valid (not executed).` when it returns true, otherwise `No installation hooks found; validation skipped.` The dependency table uses `_status_icon(dep_result.installation_valid, optional=not dep_result.installation_valid and not dep_result.errors)`.

- [ ] **Step 6: Prove the focused contract.**

Run:

```bash
uv run pytest packages/bedrock/tests/test_cli_helpers.py::TestAppsHelpers -q
uv run pytest packages/bedrock/tests/test_cli_helpers.py::TestInstallCommand::test_install_runs_all_hooks -q
```

Expected: all pass; inspection observes zero calls, invalid signatures fail, absent hook sets succeed, and install retains hook order.

### Task 2: Update CLI references

**Files:**
- Modify: `docs-web/content/docs/en/(bedrock)/guides/cli.mdx:34-63`
- Modify: `docs-web/content/docs/zh/(bedrock)/guides/cli.mdx:34-63`

- [ ] **Step 1: Update English inspect copy.** Replace its description with `Validate a module's manifest, loadability, and optional installation-hook signatures without executing installation logic.` Replace the installation-hook list item with `Present installation hooks accept the required **kwargs signature; no hook is invoked.` Replace the example with `✓ Installation hooks for 'myapp.users' are valid (not executed).` Add the note: ``installation.py` and its hooks are optional. Use `bedrock app install <import_path>` to run the installation lifecycle hooks.`

- [ ] **Step 2: Mirror the contract in Chinese.** Use `校验模块的 manifest、可加载性和可选安装钩子的签名，但不执行任何安装逻辑。`; `已存在的安装钩子是否接受必需的 **kwargs 签名；不会调用任何钩子`; and ``installation.py` 及其钩子均为可选项。请使用 `bedrock app install <import_path>` 执行安装生命周期钩子。`.

- [ ] **Step 3: Verify only the desired command distinction changed.** Run `uv run pytest packages/bedrock/tests/test_cli_helpers.py -q` and review both MDX sections for an explicit validation-only `inspect` and executing `app install` distinction.

### Task 3: Verify and review

- [ ] **Step 1: Run the complete Bedrock test suite.** Run `uv run pytest packages/bedrock/tests/`; expect no failures.
- [ ] **Step 2: Run static checks.** Run:

```bash
uv run ruff check packages/bedrock/src/bedrock/cli/apps.py packages/bedrock/tests/test_cli_helpers.py
uv run ruff format --check packages/bedrock/src/bedrock/cli/apps.py packages/bedrock/tests/test_cli_helpers.py
```

Expect both checks to pass without modifying files.

- [ ] **Step 3: Request code review.** Provide the MCK-1 acceptance criteria, final diff, and test evidence. Fix Critical and Important findings, rerun affected checks, then request explicit user approval for a conventional commit.
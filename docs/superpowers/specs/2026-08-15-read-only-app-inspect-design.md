# Read-only `bedrock app inspect` design

**Issue:** MCK-1  
**Status:** Approved

## Problem

`bedrock app inspect` must validate a module without performing installation work. Both the primary module and dependency paths currently invoke `installation.install()`. Hooks can mutate databases, create data, or call external services. A module without `installation.py` is also incorrectly reported as invalid.

## Decision

Introduce one private read-only installation-hook validator in `bedrock.cli.apps`. The primary inspection flow and dependency inspection flow will call that validator instead of duplicating hook loading and validation.

The validator loads optional `pre_install`, `install`, and `post_install` callables; validates every present callable with `_check_installation_hooks`; records whether any hook exists; and never calls a hook.

## Behavioral contract

- `bedrock app inspect <module>` validates `manifest.yaml`, module importability, optional `bootstrap` and `models` submodules, and every present installation-hook signature.
- It does not invoke `pre_install()`, `install()`, `post_install()`, or other installation logic.
- Missing `installation.py` and missing hook functions are a successful optional state.
- An invalid present hook signature is an inspection error and exits non-zero.
- Dependency inspection uses the identical validation-only behavior.
- `bedrock app install` remains unchanged and executes hooks in `pre_install` → `install` → `post_install` order.

## Test plan

- Replace the dependency test that expects `install()` to run with a zero-call regression test.
- Add the same zero-call contract for `_run_basic_inspect`.
- Verify absent hooks pass and invalid present signatures fail.
- Retain the existing install-order test.
- Run focused CLI helper tests, the complete Bedrock suite, and Ruff checks.

## Documentation

Update English and Chinese CLI references to state that `inspect` validates optional hook signatures without invoking them, and that `bedrock app install` performs lifecycle execution.

## Scope boundaries

No change to hook signatures, the lazy loader, module registry lifecycle, migration behavior, or installation execution semantics.
# bedrock-cli

`bedrock-cli` is a planned future component of the Bedrock ecosystem.

Its intended role is to provide CLI tooling for scaffolding, validation, inspection, and other developer workflows around Bedrock modular applications.

## Current status

The Bedrock runtime contract is still being refined.
Because of that, `bedrock-cli` should currently be treated as planning-in-future rather than a stable implementation target.

- Keep its purpose and responsibilities documented.
- Do not rely on the current command surface or signatures as stable contracts.
- Resume active CLI development after the runtime/module conventions are sufficiently complete and explicit.

## Intended responsibilities

The future CLI is expected to cover areas such as:

- workspace initialization
- module scaffolding
- CRUD/code generation helpers
- manifest validation
- dependency graph inspection
- project health checks

## Implementation note

The current package exists as an exploratory implementation area.
Its internal structure may change significantly once the Bedrock runtime contract is finalized.

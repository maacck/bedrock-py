# Changelog

All notable changes to `bedrock-fastapi` will be documented in this file. Bedrock core changes are in the
repository's root `CHANGELOG.md`.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Requires the Bedrock core release where `db.session` / `db.asession` no longer create a session on
  first access. Without `DbSessionMiddleware` / `AsyncDbSessionMiddleware`, reading them in a dependency
  or endpoint now raises `SessionNotBoundError` instead of creating a session that nothing closes.

## [0.1.0] - 2026-11-08

### Added

- `RequestScopeMiddleware`: each HTTP request and WebSocket connection runs inside one Bedrock DI scope.
- `DbSessionMiddleware` / `AsyncDbSessionMiddleware`: each HTTP request runs inside `db.session_scope()` /
  `db.asession_scope()`. The middleware never commits: services work in `db.session` / `db.asession` and
  commit as they do outside HTTP, and anything uncommitted when the request ends is rolled back.
- `bedrock_exception_handler(status_codes)`: answers mapped `BedrockExc` subclasses with `{"detail": ...}`.
  Unmapped Bedrock exceptions are re-raised for a generic 500 response; WebSocket errors are always re-raised.

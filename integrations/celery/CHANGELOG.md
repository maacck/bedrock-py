# Changelog

All notable changes to `bedrock-celery` will be documented in this file. Bedrock core changes are in the
repository's root `CHANGELOG.md`.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `connect_worker(app)`: connects Celery worker signals so each worker process runs `bedrock.setup()` and
  `bedrock.shutdown()`. With the prefork pool this happens in every child process after the fork, never in the
  parent; other pools set Bedrock up once in the worker process. If `bedrock.setup()` fails, the failure is
  logged and the worker stops instead of running tasks without Bedrock.
- `BedrockTask`: each task runs inside `container.scope("task")` and a bound database session
  (`db.session_scope()` for `def` tasks, `db.asession_scope()` for `async def` tasks). The task never commits:
  services commit, and anything uncommitted when the task ends is rolled back.
- `async def` tasks run with `bedrock.run_async()` on Bedrock's runtime loop, the loop async lifecycle hooks
  ran on, so they share async clients opened by those hooks. `self.request` and `self.retry()` work inside them.
  Requires `bedrock-core` 0.3.2 or later, the first release with `bedrock.run_async()`.
  A class-based task is classified by the class that defines `run`: a subclass overriding an async `run` with a
  `def run` gets a sync session.

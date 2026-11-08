# Changelog

All notable changes to `bedrock-celery` will be documented in this file. Bedrock core changes are in the
repository's root `CHANGELOG.md`.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `connect_worker(app)`: connects Celery worker signals so each worker process sets Bedrock up with
  `bedrock.asetup()` and shuts it down with `bedrock.ashutdown()`. With the prefork pool this happens in every
  child process after the fork, never in the parent; other pools set Bedrock up once in the worker process.
- `setup_worker_process(app)` / `shutdown_worker_process()`: the same per-process lifecycle, callable directly
  when tasks run without a worker (for example eagerly in tests).
- `BedrockTask`: each task runs inside `container.scope("task")` and a bound database session
  (`db.session_scope()` for `def` tasks, `db.asession_scope()` for `async def` tasks). The task never commits:
  services commit, and anything uncommitted when the task ends is rolled back.
- `async def` tasks run on the event loop the worker process was set up on, so they share async clients opened
  by lifecycle hooks. `self.request` and `self.retry()` work inside them. Workers that run async tasks use the
  prefork or solo pool; other pools raise `ImproperlyConfigured` for async tasks.

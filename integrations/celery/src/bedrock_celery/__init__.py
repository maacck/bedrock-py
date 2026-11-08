"""Celery integration for Bedrock.

The host application creates and owns the Celery app. This package provides pieces it can add: a per-process
worker lifecycle, a task base class with a per-task DI scope and database session, and async task support.
"""

from .task import BedrockTask
from .worker import connect_worker, setup_worker_process, shutdown_worker_process

__all__ = ["BedrockTask", "connect_worker", "setup_worker_process", "shutdown_worker_process"]

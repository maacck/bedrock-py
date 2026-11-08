"""Celery integration for Bedrock.

The host application creates and owns the Celery app. This package provides pieces it can add: a per-process
worker lifecycle and a task base class with a per-task DI scope and database session, including ``async def``
tasks.
"""

from .task import BedrockTask
from .worker import connect_worker

__all__ = ["BedrockTask", "connect_worker"]

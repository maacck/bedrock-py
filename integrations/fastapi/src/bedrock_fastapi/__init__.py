"""FastAPI integration for Bedrock.

The host application creates and owns its FastAPI app. Add only the pieces it needs.
"""

from .exceptions import bedrock_exception_handler
from .middleware import AsyncDbSessionMiddleware, DbSessionMiddleware, RequestScopeMiddleware

__all__ = [
    "AsyncDbSessionMiddleware",
    "DbSessionMiddleware",
    "RequestScopeMiddleware",
    "bedrock_exception_handler",
]

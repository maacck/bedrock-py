"""Pure ASGI middleware that give each request its own Bedrock DI scope and database sessions.

The middleware enter and leave request state in the request's own context. FastAPI runs sync (``def``)
dependencies and endpoints in worker threads with copied contexts, so state entered there cannot be reset
or cleaned up reliably at the end of the request.
"""

from anyio import CancelScope
from bedrock.database import db
from bedrock.di import container
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

_REQUEST_TYPES = frozenset({"http", "websocket"})


class RequestScopeMiddleware:
    """Run each HTTP or WebSocket connection inside ``container.scope("request")``.

    ``SCOPED`` services resolved while the connection is handled share one instance, and their ``close()``
    runs when it ends. Lifespan events pass through.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the downstream application.

        Args:
            app: The downstream ASGI application.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] not in _REQUEST_TYPES:
            await self.app(scope, receive, send)
            return
        with container.scope("request"):
            await self.app(scope, receive, send)


class DbSessionMiddleware:
    """Run each HTTP request inside ``db.session_scope()`` without committing on its behalf.

    Services work in ``db.session`` and commit, as they do outside HTTP. Whatever is still uncommitted when the
    request ends, including work done by background tasks, is rolled back, so a write nobody commits is lost
    while the response may still be a success.

    Because a session is already bound, ``db.session_scope()`` raises ``SessionAlreadyBoundError`` inside a request; use
    ``db.independent_session()`` for side transactions. WebSocket and lifespan events pass through.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the downstream application.

        Args:
            app: The downstream ASGI application.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.

        Raises:
            DatabaseNotConfiguredError: When ``db.init()`` has not been called.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with db.session_scope() as session:
            try:
                await self.app(scope, receive, send)
            finally:
                # Discard uncommitted work so the scope's own commit has nothing left. Shielded so a cancelled
                # request still returns its connection, and in a worker thread because the rollback is blocking I/O.
                with CancelScope(shield=True):
                    await run_in_threadpool(session.rollback)


class AsyncDbSessionMiddleware:
    """Run each HTTP request inside ``db.asession_scope()`` without committing on its behalf.

    Same contract as :class:`DbSessionMiddleware`: services commit with ``await db.asession.commit()``,
    uncommitted work is rolled back when the request ends, and ``db.asession_scope()`` raises ``SessionAlreadyBoundError``
    inside a request (use ``db.aindependent_session()``). The sync and async sessions use separate context
    variables, so both middleware can be installed together; each has its own transaction.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the downstream application.

        Args:
            app: The downstream ASGI application.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.

        Raises:
            DatabaseNotConfiguredError: When ``db.init()`` has not been called.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        async with db.asession_scope() as session:
            try:
                await self.app(scope, receive, send)
            finally:
                with CancelScope(shield=True):
                    await session.rollback()

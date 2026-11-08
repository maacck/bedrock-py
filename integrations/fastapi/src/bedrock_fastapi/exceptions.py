"""Translate Bedrock exceptions into HTTP responses."""

from collections.abc import Awaitable, Callable, Mapping

from bedrock.exc import BedrockExc
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def bedrock_exception_handler(
    status_codes: Mapping[type[BedrockExc], int],
) -> Callable[[Request, Exception], Awaitable[Response]]:
    """Build an exception handler that answers mapped Bedrock exceptions with ``{"detail": exc.detail}``.

    The lookup walks the raised exception's MRO, so mapping a module's base exception covers its subclasses
    and the most specific mapping wins. An unmapped exception is re-raised: the server-error middleware logs
    it and returns a generic 500 without exposing ``detail``, because ``BedrockExc`` is also the base of
    runtime errors such as ``ImproperlyConfigured``. WebSocket connections are never answered: an exception there
    is re-raised, because an HTTP response cannot be sent once the socket is accepted.

    Example::

        app.add_exception_handler(
            BedrockExc,
            bedrock_exception_handler(
                {ItemNotFound: 404, ItemConflict: 409}
            ),
        )

    Args:
        status_codes: HTTP status code per exception class.

    Returns:
        An async exception handler for ``app.add_exception_handler``.
    """

    async def handle(request: Request, exc: Exception) -> Response:
        if request.scope["type"] == "http" and isinstance(exc, BedrockExc):
            matches = [cls for cls in status_codes if isinstance(exc, cls)]
            if matches:
                most_specific = min(matches, key=type(exc).__mro__.index)
                return JSONResponse({"detail": exc.detail}, status_code=status_codes[most_specific])
        raise exc

    return handle

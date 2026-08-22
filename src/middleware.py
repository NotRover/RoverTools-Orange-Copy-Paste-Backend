"""Application-level middleware."""

from starlette import status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.version import API_VERSION


def _content_length(scope: Scope) -> int | None:
    """The body size the client claims, or ``None`` if it claims nothing."""
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodySizeLimitMiddleware:
    """Refuse an oversized request body before it is read into memory.

    The per-entry ceilings in ``push_entries`` are the contract, but they are
    read off an already-parsed body: by the time one of them runs, the whole
    request has been buffered and built into up to ``max_push_batch`` models.
    This answers a different question - not "is this row too large" but "is this
    worth reading at all" - so it has to sit in front of that, not beside it.

    Pure ASGI rather than ``BaseHTTPMiddleware`` because the check belongs on the
    receive channel. ``Content-Length`` is a claim the client makes, and a
    chunked request makes no claim at all, so the running total is the only
    thing that actually bounds anything.

    **Why it answers from inside the receive channel.** Raising out of
    ``receive`` does stop the read, but nobody ever sees the reason: FastAPI
    wraps every body read in ``except Exception`` and re-raises it as its own
    400 "There was an error parsing the body", so the exception never reaches
    this middleware. So the refusal is sent here, at the moment the total goes
    over, and the stream is then closed as a disconnect - which unwinds the
    route without running it. Whatever the framework says afterwards is dropped,
    because the answer has already gone out.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # WebSocket frames are bounded by the socket layer; lifespan has no body.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            # Nothing has been read at all on this path.
            await self._refuse(scope, send)
            return

        read = 0
        answered = False

        async def counting_receive() -> Message:
            nonlocal read, answered
            message = await receive()
            if message["type"] != "http.request":
                return message
            read += len(message.get("body", b""))
            if read <= self.max_bytes or answered:
                return message
            answered = True
            await self._refuse(scope, send)
            # Not a truncated body: a truncation could in principle parse, and
            # then the route would run for a request already refused. A
            # disconnect makes the read raise instead, so it cannot.
            return {"type": "http.disconnect"}

        async def guarded_send(message: Message) -> None:
            # The 413 is already on the wire, so the framework's own answer to
            # the aborted read has nowhere to go.
            if not answered:
                await send(message)

        await self.app(scope, counting_receive, guarded_send)

    async def _refuse(self, scope: Scope, send: Send) -> None:
        response = JSONResponse(
            {"detail": f"Request body is over the {self.max_bytes} byte limit."},
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )
        # `receive` is unused by a response with no background task, and there
        # is nothing left worth reading on this connection anyway.
        await response(scope, _no_receive, send)


async def _no_receive() -> Message:  # pragma: no cover - defensive
    return {"type": "http.disconnect"}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds common defensive HTTP headers + the API contract version to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response: Response = await call_next(request)
        response.headers["X-API-Version"] = API_VERSION
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        # `setdefault`, not assignment: this default suits a JSON API and would
        # block the inline style and script the HTML pages in `src/web` are built
        # from, so those routes set their own (tighter in every other respect).
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; connect-src 'self'; frame-ancestors 'none'",
        )
        # HSTS — only set in production; Tauri's http://tauri.localhost doesn't use TLS locally
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

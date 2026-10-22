from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse

MAX_REQUEST_BODY_BYTES = 1_048_576


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
                path = scope.get("path", "")
                if path.startswith("/v1/auth/") or path.endswith("/prescription"):
                    headers["Cache-Control"] = "no-store, private"
                    headers["Pragma"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


class MaxRequestBodySizeMiddleware:
    def __init__(self, app, max_bytes: int = MAX_REQUEST_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = next(
            (
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"content-length"
            ),
            None,
        )
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                await JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})(
                    scope, receive, send
                )
                return
            if declared_size < 0 or declared_size > self.max_bytes:
                await JSONResponse(
                    status_code=413, content={"detail": "Request body is too large"}
                )(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await JSONResponse(
                    status_code=413, content={"detail": "Request body is too large"}
                )(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def receive_buffered():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, receive_buffered, send)

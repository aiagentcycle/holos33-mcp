"""Origin-check tegen DNS-rebinding. Een ontbrekende Origin mag; een andere moet op de lijst staan.

Pure ASGI-middleware, zodat Streamable HTTP zijn responsestroom houdt.
"""

from __future__ import annotations

ALLOWED_ORIGINS = frozenset(
    {
        "https://mcp.holos33.com",
        "https://holos33.com",
        "http://127.0.0.1:8324",
        "http://localhost:8324",
    }
)


def origin_allowed(origin: str | None, *, enabled: bool = True) -> bool:
    if not enabled or origin is None or origin == "":
        return True
    return origin in ALLOWED_ORIGINS


class OriginGuard:
    def __init__(self, app, enabled: bool = True):
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        # Alleen het MCP-endpoint zelf. Server Card, llms.txt en Markdown zijn bedoeld voor browsers en andere sites (CORS *).
        if scope.get("type") != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return
        origin = None
        for key, value in scope.get("headers") or []:
            if key.lower() == b"origin":
                origin = value.decode("latin1")
                break
        if not origin_allowed(origin, enabled=self.enabled):
            body = b"origin rejected"
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)

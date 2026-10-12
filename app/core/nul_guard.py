"""Reject JSON/form bodies that carry NUL control characters.

NUL bytes are accepted by MySQL text columns but later break exports (python-docx refuses
them), so refuse them at the edge with a 422.
"""
import json

_METHODS = {"POST", "PUT", "PATCH"}
_MAX_BUFFER = 5 * 1024 * 1024


class RejectNulMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in _METHODS:
            return await self.app(scope, receive, send)
        ctype = dict(scope["headers"]).get(b"content-type", b"").lower()
        if b"application/json" not in ctype:
            return await self.app(scope, receive, send)

        chunks, size, more = [], 0, True
        while more:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            chunks.append(msg.get("body", b""))
            size += len(chunks[-1])
            more = msg.get("more_body", False)
            if size > _MAX_BUFFER:
                break
        body = b"".join(chunks)

        if size <= _MAX_BUFFER and (b"\x00" in body or b"\\u0000" in body.lower()):
            payload = json.dumps({"success": False, "error": "Null characters are not allowed",
                                  "code": "VALIDATION_ERROR"}).encode()
            await send({"type": "http.response.start", "status": 422,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(payload)).encode())]})
            await send({"type": "http.response.body", "body": payload})
            return

        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": more}
            return await receive()

        await self.app(scope, replay, send)

"""Read request bodies with a hard limit before allocating their full size."""

from fastapi import Request

from app.errors import ApiError


async def bounded_body(request: Request, limit: int) -> bytes:
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise ApiError(413, "payload_too_large", "request body exceeds the size limit")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "payload_too_large", "request body exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)

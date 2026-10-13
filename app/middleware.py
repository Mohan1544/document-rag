"""Bound incoming bodies before multipart parsing and normalize request failures."""

import asyncio
import logging
import time
from tempfile import SpooledTemporaryFile
from uuid import uuid4

from fastapi import Request
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse

from app.config import Settings
from app.errors import ServiceError
from app.observability import RequestMetrics, current_metrics

logger = logging.getLogger("document_rag")


def error_response(status: int, code: str, message: str, request_id: str) -> JSONResponse:
    headers = {"X-Request-ID": request_id}
    if code in {"service_busy", "index_busy", "service_stopping"}:
        headers["Retry-After"] = "1"
    return JSONResponse(
        status_code=status,
        content={"request_id": request_id, "error": {"code": code, "message": message}},
        headers=headers,
    )


class LimitedUploadRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            if request.url.path != "/api/v1/answer":
                return await original(request)
            # Parse once with a strict file/field count; FastAPI reuses the cached
            # FormData for its declared UploadFile parameters. Always close spools.
            async with request.form(max_files=2, max_fields=0) as form:
                if any(name not in {"questions_file", "document_file"} for name in form):
                    raise ServiceError(
                        422, "unexpected_upload", "Upload only the two required files."
                    )
                if any(len(form.getlist(name)) != 1 for name in form):
                    raise ServiceError(
                        422, "duplicate_upload", "Each upload field must occur once."
                    )
                return await original(request)

        return handle


class RequestBoundaryMiddleware:
    """ASGI boundary; a dishonest/missing Content-Length cannot bypass byte limits."""

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self._active = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        metrics = RequestMetrics(request_id)
        token = current_metrics.set(metrics)
        started = time.monotonic()
        status = 500
        response_started = False
        deadline = None
        expensive = scope["path"] in {"/api/v1/answer", "/api/v1/export.xlsx"}
        admitted = False

        async def tracked_send(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                message["headers"] = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ] + [(b"x-request-id", request_id.encode())]
                # The processing deadline ends when a complete response is ready;
                # slow response transmission must not trigger a second response.
                if deadline is not None:
                    deadline.reschedule(None)
            await send(message)

        try:
            if expensive:
                if self._active >= self.settings.max_active_requests:
                    raise ServiceError(
                        503, "service_busy", "The service is busy. Try again shortly."
                    )
                self._active += 1
                admitted = True
                limit = (
                    self.settings.max_document_bytes + self.settings.max_questions_bytes + 100_000
                    if scope["path"] == "/api/v1/answer"
                    else self.settings.max_export_bytes
                )
                length_header = next(
                    (
                        value
                        for name, value in scope["headers"]
                        if name.lower() == b"content-length"
                    ),
                    None,
                )
                if length_header is not None:
                    try:
                        declared = int(length_header)
                    except ValueError as exc:
                        raise ServiceError(
                            400, "invalid_content_length", "Invalid Content-Length."
                        ) from exc
                    if declared < 0:
                        raise ServiceError(400, "invalid_content_length", "Invalid Content-Length.")
                    if declared > limit:
                        raise ServiceError(413, "request_too_large", "Request is too large.")
                async with asyncio.timeout(self.settings.request_timeout_seconds) as deadline:
                    # A bounded spool precedes multipart parsing and is removed on
                    # every exit, including oversized/slow bodies and cancellation.
                    with SpooledTemporaryFile(max_size=1_000_000) as body:
                        size = 0
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                status = 499
                                return
                            chunk = message.get("body", b"")
                            size += len(chunk)
                            if size > limit:
                                raise ServiceError(
                                    413, "request_too_large", "Request is too large."
                                )
                            await asyncio.to_thread(body.write, chunk)
                            if not message.get("more_body", False):
                                break
                        await asyncio.to_thread(body.seek, 0)

                        async def replay():
                            chunk = await asyncio.to_thread(body.read, 65_536)
                            return {
                                "type": "http.request",
                                "body": chunk,
                                "more_body": body.tell() < size,
                            }

                        processing = asyncio.create_task(self.app(scope, replay, tracked_send))

                        async def watch_disconnect():
                            while True:
                                if (await receive())["type"] == "http.disconnect":
                                    return

                        disconnected = asyncio.create_task(watch_disconnect())
                        try:
                            completed, _ = await asyncio.wait(
                                [processing, disconnected], return_when=asyncio.FIRST_COMPLETED
                            )
                            if processing in completed:
                                await processing
                            else:
                                status = 499
                        finally:
                            for task in (processing, disconnected):
                                if not task.done():
                                    task.cancel()
                            await asyncio.gather(processing, disconnected, return_exceptions=True)
            else:
                await self.app(scope, receive, tracked_send)
        except TimeoutError:
            deadline = None
            if response_started:
                raise
            await error_response(
                504, "request_timeout", "The request took too long to process.", request_id
            )(scope, receive, tracked_send)
        except ServiceError as exc:
            deadline = None
            if response_started:
                raise
            await error_response(exc.status_code, exc.code, exc.message, request_id)(
                scope, receive, tracked_send
            )
        except Exception as exc:
            deadline = None
            logger.error("request_failed", extra={"error_type": type(exc).__name__})
            if response_started:
                raise
            await error_response(
                500, "internal_error", "The request could not be completed.", request_id
            )(scope, receive, tracked_send)
        finally:
            if admitted:
                self._active -= 1
            logger.info(
                "http_request",
                extra={
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "summary": metrics.summary(),
                },
            )
            current_metrics.reset(token)

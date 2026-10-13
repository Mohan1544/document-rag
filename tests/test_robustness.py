import asyncio
import json
import logging
from io import StringIO

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from app.api import create_app
from app.config import Settings
from app.middleware import RequestBoundaryMiddleware
from app.observability import JsonFormatter
from tests.conftest import FakeProvider


async def raw_request(app, chunks, headers=()):
    iterator = iter(chunks)
    messages = []

    async def receive():
        try:
            body = next(iterator)
        except StopIteration:
            await asyncio.Event().wait()
        return {"type": "http.request", "body": body, "more_body": True}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": "/api/v1/answer",
        "raw_path": b"/api/v1/answer",
        "query_string": b"",
        "headers": list(headers),
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
        "root_path": "",
    }
    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return start, json.loads(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [(), ((b"content-length", b"1"),)])
async def test_actual_stream_limit_precedes_multipart_even_with_false_length(headers) -> None:
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True
        await JSONResponse({"ok": True})(scope, receive, send)

    app = RequestBoundaryMiddleware(
        downstream, Settings(max_document_bytes=1, max_questions_bytes=1)
    )
    start, body = await raw_request(app, [b"x" * 65_536, b"x" * 40_000], headers)
    assert start["status"] == 413
    assert body["error"]["code"] == "request_too_large"
    assert called is False
    assert dict(start["headers"])[b"x-request-id"].decode() == body["request_id"]
    assert app._active == 0


@pytest.mark.asyncio
async def test_request_deadline_includes_upload_body() -> None:
    async def downstream(scope, receive, send):
        raise AssertionError("Incomplete upload must never reach the application")

    app = RequestBoundaryMiddleware(downstream, Settings(request_timeout_seconds=1))
    start, body = await raw_request(app, [b"incomplete"])
    assert start["status"] == 504
    assert body["error"]["code"] == "request_timeout"
    assert app._active == 0


@pytest.mark.asyncio
async def test_admission_rejects_busy_request_before_reading_body() -> None:
    async def downstream(scope, receive, send):
        raise AssertionError("Busy request must not reach the application")

    app = RequestBoundaryMiddleware(downstream, Settings(max_active_requests=1))
    app._active = 1
    start, body = await raw_request(app, [])
    assert start["status"] == 503
    assert body["error"]["code"] == "service_busy"
    assert dict(start["headers"])[b"retry-after"] == b"1"
    assert app._active == 1


def test_error_envelopes_and_readiness_are_consistent(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(create_app()) as client:
        for method, path, status in [
            ("post", "/api/v1/answer", 422),
            ("get", "/missing", 404),
            ("get", "/api/v1/answer", 405),
            ("get", "/readyz", 503),
        ]:
            response = getattr(client, method)(path)
            assert response.status_code == status
            assert response.json()["error"]["message"]
            assert response.json()["request_id"] == response.headers["x-request-id"]
        assert client.get("/healthz").status_code == 200


def test_unexpected_exception_is_safe_json() -> None:
    class BrokenProvider(FakeProvider):
        async def embed(self, texts):
            raise RuntimeError("private document text sk-secret")

    with TestClient(create_app(provider=BrokenProvider())) as client:
        response = client.post(
            "/api/v1/answer",
            files={
                "questions_file": ("questions.json", b'["Cloud?"]'),
                "document_file": ("source.json", b'{"provider":"GCP"}'),
            },
        )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private" not in response.text and "sk-secret" not in response.text


def test_multipart_limits_reject_excess_and_duplicate_files() -> None:
    provider = FakeProvider()
    with TestClient(create_app(provider=provider)) as client:
        three = client.post(
            "/api/v1/answer",
            files=[
                ("questions_file", ("q.json", b'["Cloud?"]')),
                ("document_file", ("s.json", b'{"provider":"GCP"}')),
                ("extra", ("extra.json", b"{}")),
            ],
        )
        assert three.status_code == 400
        duplicate = client.post(
            "/api/v1/answer",
            files=[
                ("questions_file", ("q.json", b'["Cloud?"]')),
                ("questions_file", ("q2.json", b'["Cloud?"]')),
            ],
        )
        assert duplicate.status_code == 422
        assert duplicate.json()["error"]["code"] == "duplicate_upload"
    assert provider.embedding_batches == []


def test_request_timeout_cancels_shared_indexing() -> None:
    cancelled = []

    class WaitingProvider(FakeProvider):
        async def embed(self, texts):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    app = create_app(Settings(request_timeout_seconds=1), WaitingProvider())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/answer",
            files={
                "questions_file": ("q.json", b'["Cloud?"]'),
                "document_file": ("s.json", b'{"provider":"GCP"}'),
            },
        )
        assert response.status_code == 504
    assert cancelled
    assert not app.state.cache._inflight


def test_correlated_logs_include_cache_outcomes_and_stage_times() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("document_rag")
    logger.addHandler(handler)
    try:
        with TestClient(create_app(provider=FakeProvider())) as client:
            for _ in range(2):
                response = client.post(
                    "/api/v1/answer",
                    files={
                        "questions_file": ("q.json", b'["Cloud?"]'),
                        "document_file": ("s.json", b'{"provider":"GCP"}'),
                    },
                )
                assert response.status_code == 200
        logs = [json.loads(line) for line in stream.getvalue().splitlines()]
        completed = [item for item in logs if item["event"] == "answer_completed"]
        assert [item["summary"]["cache_status"] for item in completed] == ["miss", "hit"]
        assert completed[0]["summary"]["answered"] == 1
        assert "extraction" in completed[0]["summary"]["stage_ms"]
        assert all(item.get("request_id") for item in logs)
        assert "$.provider" not in stream.getvalue()
    finally:
        logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_disconnect_cancels_application_work() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    request_received = False

    async def downstream(scope, receive, send):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def receive():
        nonlocal request_received
        if not request_received:
            request_received = True
            return {"type": "http.request", "body": b"body", "more_body": False}
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        raise AssertionError("Disconnected request must not send a response")

    app = RequestBoundaryMiddleware(downstream, Settings())
    await app({"type": "http", "path": "/api/v1/answer", "headers": []}, receive, send)
    assert cancelled.is_set()
    assert app._active == 0

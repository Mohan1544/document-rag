import asyncio
import json
import threading
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api import create_app
from app.config import Settings
from app.retrieval import SearchIndex
from tests.conftest import FakeProvider


def _files(questions: bytes, document: bytes, source_name: str = "source.json"):
    return {
        "questions_file": ("questions.json", questions, "application/json"),
        "document_file": (source_name, document, "application/octet-stream"),
    }


def test_endpoint_handles_multiple_questions_and_export(fake_provider: FakeProvider) -> None:
    client = TestClient(create_app(provider=fake_provider))
    questions = json.dumps(
        {"questions": ["Which cloud provider?", "Unknown backup region?"]}
    ).encode()
    response = client.post(
        "/api/v1/answer",
        files=_files(questions, b'{"hosting":{"provider":"GCP"}}'),
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["status"] == "answered"
    assert body["results"][0]["citations"][0]["json_path"] == "$.hosting"
    assert body["results"][1]["status"] == "not_found"
    assert response.headers["x-request-id"] == body["request_id"]

    exported = client.post("/api/v1/export.xlsx", json={"results": body["results"]})
    assert exported.status_code == 200
    sheet = load_workbook(BytesIO(exported.content), read_only=True).active
    assert [cell.value for cell in sheet[1]] == [
        "id",
        "question",
        "answer",
        "comments",
        "confidence",
        "status",
        "citations",
    ]
    assert sheet[2][2].value == "Google Cloud Platform"


def test_endpoint_accepts_pdf_and_returns_page_citation(
    fake_provider: FakeProvider, pdf_bytes: bytes
) -> None:
    client = TestClient(create_app(provider=fake_provider))
    response = client.post(
        "/api/v1/answer",
        files=_files(b'["Which cloud provider?"]', pdf_bytes, "report.pdf"),
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["answer"] == "Google Cloud Platform"
    assert result["citations"][0]["page"] == 2


def test_endpoint_rejects_invalid_question_json_without_key(fake_provider: FakeProvider) -> None:
    client = TestClient(create_app(provider=fake_provider))
    response = client.post("/api/v1/answer", files=_files(b"{broken", b'{"cloud":"GCP"}'))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_questions_json"


def test_endpoint_enforces_upload_limit(fake_provider: FakeProvider) -> None:
    client = TestClient(create_app(settings=Settings(max_document_bytes=8), provider=fake_provider))
    response = client.post("/api/v1/answer", files=_files(b'["Question?"]', b'{"cloud":"GCP"}'))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "document_too_large"


def test_export_escapes_spreadsheet_formulas(fake_provider: FakeProvider) -> None:
    client = TestClient(create_app(provider=fake_provider))
    payload = {
        "results": [
            {
                "id": "1",
                "question": '=HYPERLINK("bad")',
                "answer": "No",
                "comments": "",
                "confidence": "low",
                "status": "not_found",
                "citations": [],
            }
        ]
    }
    response = client.post("/api/v1/export.xlsx", json=payload)
    sheet = load_workbook(BytesIO(response.content), read_only=True).active
    assert sheet[2][1].value.startswith("'=HYPERLINK")


@pytest.mark.asyncio
async def test_health_stays_responsive_during_slow_search(
    fake_provider: FakeProvider, monkeypatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    original_search = SearchIndex.search

    def slow_search(self, question, embedding, top_k):
        started.set()
        release.wait(timeout=1)
        return original_search(self, question, embedding, top_k)

    monkeypatch.setattr(SearchIndex, "search", slow_search)
    app = create_app(provider=fake_provider)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        answer_task = asyncio.create_task(
            client.post(
                "/api/v1/answer",
                files=_files(b'["Which cloud?"]', b'{"provider":"GCP"}'),
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 0.5)
            health = await asyncio.wait_for(client.get("/healthz"), timeout=0.3)
            assert health.status_code == 200
            assert not answer_task.done()
        finally:
            release.set()
            await answer_task

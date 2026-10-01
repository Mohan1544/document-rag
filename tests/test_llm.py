import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.config import Settings
from app.errors import ServiceError
from app.llm import OpenAIProvider
from app.schemas import Passage


@pytest.mark.asyncio
async def test_openai_payloads_use_required_models_and_structured_citations(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    seen: list[tuple[str, dict]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]},
                        {"object": "embedding", "index": 1, "embedding": [0.0, 1.0]},
                    ],
                    "model": "text-embedding-3-small",
                    "usage": {"prompt_tokens": 5, "total_tokens": 5},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "answer": "GCP",
                                    "comments": "The passage names GCP.",
                                    "citation_ids": ["p1"],
                                    "is_answerable": True,
                                    "evidence_strength": "high",
                                }
                            ),
                            "refusal": None,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        provider = OpenAIProvider(Settings())
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key="test-key", http_client=http_client)
        vectors = await provider.embed(["first", "second"])
        draft = await provider.answer(
            "Which cloud?", [Passage("p1", "The service runs on GCP.", "source.pdf", page=2)]
        )
        await provider.client.close()

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert draft.answer == "GCP"
    assert draft.citation_ids == ["p1"]
    assert seen[0][1]["model"] == "text-embedding-3-small"
    assert seen[1][1]["model"] == "gpt-4o-mini"
    assert seen[1][1]["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (400, "model_bad_request"),
        (401, "invalid_api_key"),
        (403, "model_access_denied"),
        (429, "model_rate_limited"),
    ],
)
async def test_embedding_api_errors_are_actionable_and_do_not_expose_upstream_text(
    monkeypatch, caplog, status: int, expected_code: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={
                "error": {
                    "message": "upstream detail sk-secretvalue",
                    "type": "invalid_request_error",
                    "code": "invalid_input",
                    "param": "input",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http_client:
        provider = OpenAIProvider(Settings())
        await provider.client.close()
        provider.client = AsyncOpenAI(api_key="test-key", http_client=http_client, max_retries=0)
        with pytest.raises(ServiceError) as raised:
            await provider.embed(["sample passage"])
        await provider.client.close()

    assert raised.value.code == expected_code
    assert "sk-secretvalue" not in raised.value.message
    assert all("sk-secretvalue" not in record.getMessage() for record in caplog.records)

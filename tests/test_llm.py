import asyncio
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
                                    "evidence_quotes": [{"passage_id": "p1", "quote": "GCP"}],
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
        provider = OpenAIProvider(
            Settings(), client=AsyncOpenAI(api_key="test-key", http_client=http_client)
        )
        vectors = await provider.embed(["first", "second"])
        draft = await provider.answer(
            "Which cloud?", [Passage("p1", "The service runs on GCP.", "source.pdf", page=2)]
        )
        await provider.aclose()

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
        provider = OpenAIProvider(
            Settings(),
            client=AsyncOpenAI(api_key="test-key", http_client=http_client, max_retries=0),
        )
        with pytest.raises(ServiceError) as raised:
            await provider.embed(["sample passage"])
        await provider.aclose()

    assert raised.value.code == expected_code
    assert "sk-secretvalue" not in raised.value.message
    assert all("sk-secretvalue" not in record.getMessage() for record in caplog.records)


def _completion(content, finish_reason="stop", refusal=None):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
    }


def _answer_json(**changes):
    return json.dumps(
        {
            "answer": "GCP",
            "comments": "Source names GCP.",
            "citation_ids": ["p1"],
            "is_answerable": True,
            "evidence_strength": "high",
            "evidence_quotes": [{"passage_id": "p1", "quote": "GCP"}],
            **changes,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({**_completion(_answer_json()), "choices": []}, "invalid_model_response"),
        ({**_completion(_answer_json()), "choices": None}, "invalid_model_response"),
        (_completion(None), "invalid_model_response"),
        (_completion(None, refusal="private upstream text"), "model_refused"),
        (_completion(_answer_json(), finish_reason="length"), "model_response_incomplete"),
        (_completion(None, finish_reason="content_filter"), "model_refused"),
        (_completion('{"answer":"unfinished"'), "invalid_model_response"),
        (_completion(_answer_json(is_answerable="true")), "invalid_model_response"),
        (_completion(_answer_json(answer="x" * 2001)), "invalid_model_response"),
        (_completion(_answer_json(comments="x" * 2001)), "invalid_model_response"),
        (_completion(_answer_json(answer="", citation_ids=[])), "invalid_model_response"),
        (_completion(_answer_json(unexpected="field")), "invalid_model_response"),
    ],
)
async def test_invalid_completions_are_errors_not_missing_evidence(
    mock_openai_provider, payload, code
) -> None:
    async with mock_openai_provider(lambda request: httpx.Response(200, json=payload)) as provider:
        with pytest.raises(ServiceError) as raised:
            await provider.answer("Which cloud?", [Passage("p1", "GCP", "source.json")])
    assert raised.value.code == code
    assert "private upstream text" not in raised.value.message


@pytest.mark.asyncio
async def test_completed_unsupported_answer_remains_not_found(mock_openai_provider) -> None:
    payload = _completion(
        _answer_json(
            answer="Data-Not-Found", is_answerable=False, citation_ids=[], evidence_strength="low"
        )
    )
    async with mock_openai_provider(lambda request: httpx.Response(200, json=payload)) as provider:
        draft = await provider.answer("Backup region?", [Passage("p1", "GCP", "source.json")])
    assert draft.is_answerable is False


def _embedding_response(data):
    return {
        "object": "list",
        "model": "text-embedding-3-small",
        "data": data,
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [1, 0]}, {"index": 2, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [1]}],
        [{"index": 0, "embedding": []}, {"index": 1, "embedding": []}],
        [{"index": 0, "embedding": [0, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [float("nan"), 1]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": ["invalid", 1]}, {"index": 1, "embedding": [0, 1]}],
    ],
)
async def test_embedding_batches_validate_indices_and_vectors(mock_openai_provider, data) -> None:
    async with mock_openai_provider(
        lambda request: httpx.Response(
            200,
            content=json.dumps(_embedding_response(data)),
            headers={"content-type": "application/json"},
        )
    ) as provider:
        with pytest.raises(ServiceError) as raised:
            await provider.embed(["first", "second"])
    assert raised.value.code == "invalid_embedding_response"


@pytest.mark.asyncio
async def test_embeddings_preserve_order_across_reordered_batches(mock_openai_provider) -> None:
    seen = []

    def respond(request):
        body = json.loads(request.content)
        assert body["encoding_format"] == "float"
        seen.append(len(body["input"]))
        return httpx.Response(
            200,
            json=_embedding_response(
                [
                    {"index": i, "embedding": [float(text), 1.0]}
                    for i, text in reversed(list(enumerate(body["input"])))
                ]
            ),
        )

    async with mock_openai_provider(respond) as provider:
        vectors = await provider.embed([str(i) for i in range(33)])
    assert vectors == [[float(i), 1.0] for i in range(33)]
    assert seen == [32, 1]


@pytest.mark.asyncio
async def test_embedding_width_cannot_change_between_batches(mock_openai_provider) -> None:
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_embedding_response(
                [
                    {"index": i, "embedding": [1.0] * (2 if calls == 1 else 3)}
                    for i in range(len(body["input"]))
                ]
            ),
        )

    async with mock_openai_provider(respond) as provider:
        with pytest.raises(ServiceError) as raised:
            await provider.embed(["text"] * 33)
    assert raised.value.code == "invalid_embedding_response"


@pytest.mark.asyncio
async def test_embedding_batches_share_concurrency_limit_and_preserve_input_order(
    mock_openai_provider,
) -> None:
    active = peak = 0

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            texts = json.loads(request.content)["input"]
            return httpx.Response(
                200,
                json=_embedding_response(
                    [
                        {"index": i, "embedding": [float(text), 1.0]}
                        for i, text in reversed(list(enumerate(texts)))
                    ]
                ),
            )
        finally:
            active -= 1

    async with mock_openai_provider(respond) as provider:
        first, second = await asyncio.gather(
            provider.embed([str(i) for i in range(96)]),
            provider.embed([str(i) for i in range(96, 192)]),
        )
    assert peak == 3
    assert active == 0
    assert first + second == [[float(i), 1.0] for i in range(192)]


@pytest.mark.asyncio
async def test_failed_embedding_batch_cancels_other_batches(mock_openai_provider) -> None:
    others_started = asyncio.Event()
    waiting = cancelled = 0

    async def respond(request):
        nonlocal waiting, cancelled
        first = json.loads(request.content)["input"][0]
        if first == "0":
            await others_started.wait()
            return httpx.Response(
                400, json={"error": {"message": "Rejected", "code": "invalid_input"}}
            )
        waiting += 1
        if waiting == 2:
            others_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled += 1

    async with mock_openai_provider(respond) as provider:
        with pytest.raises(ServiceError) as raised:
            await asyncio.wait_for(provider.embed([str(i) for i in range(96)]), timeout=3)
    assert raised.value.code == "model_bad_request"
    assert cancelled == 2


@pytest.mark.asyncio
async def test_request_usage_totals_include_embeddings_and_answers(mock_openai_provider) -> None:
    from app.observability import RequestMetrics, current_metrics

    def respond(request):
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200, json=_embedding_response([{"index": 0, "embedding": [1, 1]}])
            )
        return httpx.Response(
            200,
            json={
                **_completion(_answer_json()),
                "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17},
            },
        )

    metrics = RequestMetrics("request-test")
    token = current_metrics.set(metrics)
    try:
        async with mock_openai_provider(respond) as provider:
            await provider.embed(["GCP"])
            await provider.answer("Cloud?", [Passage("p1", "GCP", "source.json")])
    finally:
        current_metrics.reset(token)
    totals = metrics.summary()
    assert totals["model_calls"] == 2
    assert totals["total_tokens"] == 22
    assert totals["embedding_tokens"] == 5
    assert totals["prompt_tokens"] == 10 and totals["completion_tokens"] == 7

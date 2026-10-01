import asyncio
import json

import pytest

from app.config import Settings
from app.errors import ServiceError
from app.schemas import DraftAnswer, Question
from app.service import DocumentQAService
from tests.conftest import FakeProvider


@pytest.mark.asyncio
async def test_multiple_answers_order_concurrency_and_cached_index(
    fake_provider: FakeProvider,
) -> None:
    settings = Settings(max_concurrent_answers=2)
    service = DocumentQAService(fake_provider, settings)
    source = json.dumps({"cloud": {"provider": "GCP"}}).encode()
    questions = [Question(str(i), f"Cloud provider question {i}?") for i in range(5)]
    _, results = await service.answer(source, "source.json", questions)
    assert [result.id for result in results] == [str(i) for i in range(5)]
    assert all(result.status == "answered" for result in results)
    assert fake_provider.max_active == 2
    assert fake_provider.embedding_batches == [1, 5]

    await service.answer(source, "source.json", [Question("followup", "Cloud provider?")])
    assert fake_provider.embedding_batches == [1, 5, 1]


@pytest.mark.asyncio
async def test_unknown_citation_is_not_accepted(fake_provider: FakeProvider) -> None:
    async def invalid_answer(question, passages):
        return DraftAnswer("Invented", "Unsupported", ["not-a-real-passage"], True, "high")

    fake_provider.answer = invalid_answer
    service = DocumentQAService(fake_provider, Settings())
    _, results = await service.answer(b'{"cloud":"GCP"}', "source.json", [Question("q1", "Cloud?")])
    assert results[0].status == "not_found"
    assert results[0].answer == "Data-Not-Found"
    assert results[0].citations == []


@pytest.mark.asyncio
async def test_duplicate_question_text_uses_one_model_call(fake_provider: FakeProvider) -> None:
    service = DocumentQAService(fake_provider, Settings())
    _, results = await service.answer(
        b'{"provider":"GCP"}',
        "source.json",
        [Question("first", "Which cloud?"), Question("second", "Which cloud?")],
    )
    assert [result.id for result in results] == ["first", "second"]
    assert fake_provider.embedding_batches == [1, 1]
    assert fake_provider.answer_calls == 1


@pytest.mark.asyncio
async def test_simultaneous_uploads_share_one_index_build() -> None:
    class SlowProvider(FakeProvider):
        source_embedding_calls = 0

        async def embed(self, texts: list[str]) -> list[list[float]]:
            if "GCP" in texts[0]:
                self.source_embedding_calls += 1
                await asyncio.sleep(0.02)
            return await super().embed(texts)

    provider = SlowProvider()
    service = DocumentQAService(provider, Settings())
    source = b'{"provider":"GCP"}'
    await asyncio.gather(
        service.answer(source, "source.json", [Question("one", "Which cloud?")]),
        service.answer(source, "source.json", [Question("two", "Which cloud?")]),
    )
    assert provider.source_embedding_calls == 1


@pytest.mark.asyncio
async def test_failed_index_build_can_be_retried() -> None:
    class FlakyProvider(FakeProvider):
        source_embedding_calls = 0

        async def embed(self, texts: list[str]) -> list[list[float]]:
            if "GCP" in texts[0]:
                self.source_embedding_calls += 1
                if self.source_embedding_calls == 1:
                    raise ServiceError(503, "temporary_failure", "Try again.")
            return await super().embed(texts)

    provider = FlakyProvider()
    service = DocumentQAService(provider, Settings())
    source = b'{"provider":"GCP"}'
    questions = [Question("one", "Which cloud?")]
    with pytest.raises(ServiceError):
        await service.answer(source, "source.json", questions)
    _, results = await service.answer(source, "source.json", questions)
    assert results[0].status == "answered"
    assert provider.source_embedding_calls == 2

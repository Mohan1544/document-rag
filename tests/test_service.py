import asyncio
import json

import pytest

from app.config import Settings
from app.errors import ServiceError
from app.schemas import DraftAnswer, Passage, Question
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
    assert results[0].status == "error"
    assert results[0].answer is None
    assert results[0].error.code == "invalid_model_citation"
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


@pytest.mark.asyncio
async def test_one_question_error_preserves_success_and_duplicate_ids() -> None:
    class MixedProvider(FakeProvider):
        async def answer(self, question, passages):
            if "fail" in question:
                self.answer_calls += 1
                raise ServiceError(504, "model_timeout", "Answer service timed out.")
            return await super().answer(question, passages)

    provider = MixedProvider()
    _, results = await DocumentQAService(provider, Settings()).answer(
        b'{"provider":"GCP"}',
        "source.json",
        [
            Question("a", "Which cloud?"),
            Question("b", "fail"),
            Question("c", "Unknown backup?"),
            Question("d", "fail"),
        ],
    )
    assert [result.id for result in results] == ["a", "b", "c", "d"]
    assert [result.status for result in results] == ["answered", "error", "not_found", "error"]
    assert provider.answer_calls == 3
    assert results[1].answer is None and results[1].confidence is None
    assert results[1].error.code == "model_timeout"


@pytest.mark.asyncio
async def test_question_timeout_preserves_other_answers_and_cancels_model() -> None:
    cancelled = asyncio.Event()

    class SlowProvider(FakeProvider):
        async def answer(self, question, passages):
            if "slow" in question:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return await super().answer(question, passages)

    _, results = await DocumentQAService(
        SlowProvider(), Settings(question_timeout_seconds=1)
    ).answer(
        b'{"provider":"GCP"}',
        "source.json",
        [Question("a", "Which cloud?"), Question("b", "slow")],
    )
    assert results[0].status == "answered"
    assert results[1].error.code == "question_timeout"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_unexpected_question_failure_does_not_expose_exception() -> None:
    class BrokenProvider(FakeProvider):
        async def answer(self, question, passages):
            raise RuntimeError("private source and credentials")

    _, results = await DocumentQAService(BrokenProvider(), Settings()).answer(
        b'{"provider":"GCP"}',
        "source.json",
        [Question("q", "Which cloud?")],
    )
    assert results[0].error.code == "question_processing_failed"
    assert "private" not in results[0].model_dump_json()


@pytest.mark.asyncio
async def test_overlong_answer_is_an_error_instead_of_silently_truncated() -> None:
    class VerboseProvider(FakeProvider):
        async def answer(self, question, passages):
            return DraftAnswer("x" * 2001, "", [passages[0].id], True, "high")

    _, results = await DocumentQAService(VerboseProvider(), Settings()).answer(
        b'{"provider":"GCP"}',
        "source.json",
        [Question("q", "Which cloud?")],
    )
    assert results[0].error.code == "invalid_model_response"
    assert results[0].answer is None


@pytest.mark.asyncio
async def test_invalid_document_embeddings_fail_shared_pipeline_before_answering() -> None:
    class BrokenProvider(FakeProvider):
        async def embed(self, texts):
            return [[float("inf"), 1.0] for text in texts]

    provider = BrokenProvider()
    with pytest.raises(ServiceError) as raised:
        await DocumentQAService(provider, Settings()).answer(
            b'{"provider":"GCP"}',
            "source.json",
            [Question("q", "Which cloud?")],
        )
    assert raised.value.code == "invalid_embedding_response"
    assert provider.answer_calls == 0


@pytest.mark.asyncio
async def test_request_cancellation_is_not_converted_to_question_error() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def answer(self, question, passages):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    service = DocumentQAService(WaitingProvider(), Settings())
    task = asyncio.create_task(
        service.answer(b'{"provider":"GCP"}', "source.json", [Question("q", "Which cloud?")])
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.parametrize(
    "quotes",
    [
        [],
        [{"passage_id": "p1", "quote": "AWS"}],
        [{"passage_id": "p2", "quote": "GCP"}],
        [{"passage_id": "p1", "quote": "GCP"}, {"passage_id": "p1", "quote": "GCP"}],
        [{"passage_id": "p1", "quote": "   "}],
    ],
)
def test_answer_rejects_missing_fabricated_or_duplicate_quotes(quotes) -> None:
    with pytest.raises(ServiceError) as raised:
        DocumentQAService._ground_result(
            Question("q", "Cloud?"),
            DraftAnswer("GCP", "", ["p1"], True, "high", quotes),
            [Passage("p1", "The cloud is GCP.", "source.json")],
        )
    assert raised.value.code == "invalid_model_quote"


def test_supporting_quote_tolerates_pdf_line_breaks() -> None:
    result = DocumentQAService._ground_result(
        Question("q", "Cloud?"),
        DraftAnswer(
            "GCP",
            "",
            ["p1"],
            True,
            "high",
            [
                {"passage_id": "p1", "quote": "cloud is GCP"},
            ],
        ),
        [Passage("p1", "The cloud\nis  GCP.", "source.pdf", page=2)],
    )
    assert result.status == "answered"
    assert result.citations[0].quote == "cloud is GCP"

"""Orchestrate extraction, indexing, retrieval, and bounded answer calls."""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from hashlib import sha256

from app.config import Settings
from app.llm import ModelProvider
from app.parsing import parse_document
from app.retrieval import SearchIndex
from app.schemas import AnswerResult, Citation, DocumentSummary, Question

logger = logging.getLogger("document_rag")


class IndexCache:
    """Bounded, process-local LRU cache for parsed and embedded documents."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._items: OrderedDict[str, tuple[float, str, SearchIndex]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[tuple[str, SearchIndex]]] = {}
        self._lock = asyncio.Lock()

    async def get_or_build(
        self,
        key: str,
        builder: Callable[[], Awaitable[tuple[str, SearchIndex]]],
    ) -> tuple[str, SearchIndex]:
        """Share one parse and embedding task across simultaneous identical uploads."""
        async with self._lock:
            entry = self._items.get(key)
            if entry is not None:
                expires_at, document_type, index = entry
                if expires_at >= time.monotonic():
                    self._items.move_to_end(key)
                    return document_type, index
                del self._items[key]
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._build(key, builder))
                task.add_done_callback(self._observe_build)
                self._inflight[key] = task
        # A cancelled request must not cancel indexing needed by another waiter.
        return await asyncio.shield(task)

    @staticmethod
    def _observe_build(task: asyncio.Task[tuple[str, SearchIndex]]) -> None:
        # Retrieve failures even if every waiting request timed out or disconnected.
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.warning("index_build_failed", extra={"error_type": type(error).__name__})

    async def _build(
        self, key: str, builder: Callable[[], Awaitable[tuple[str, SearchIndex]]]
    ) -> tuple[str, SearchIndex]:
        try:
            document_type, index = await builder()
            await self.put(key, document_type, index)
            return document_type, index
        finally:
            async with self._lock:
                self._inflight.pop(key, None)

    async def put(self, key: str, document_type: str, index: SearchIndex) -> None:
        async with self._lock:
            self._items[key] = (
                time.monotonic() + self.settings.cache_ttl_seconds,
                document_type,
                index,
            )
            self._items.move_to_end(key)
            while len(self._items) > self.settings.cache_entries:
                self._items.popitem(last=False)


class DocumentQAService:
    """Answer uploaded questions while enforcing retrieval and citation boundaries."""

    def __init__(
        self,
        provider: ModelProvider,
        settings: Settings,
        cache: IndexCache | None = None,
        answer_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.cache = cache or IndexCache(settings)
        self.answer_semaphore = answer_semaphore or asyncio.Semaphore(
            settings.max_concurrent_answers
        )

    async def _index(self, document: bytes, filename: str) -> tuple[str, SearchIndex]:
        # The filename affects citation metadata, so identical bytes under another name
        # need a separate cached index.
        cache_key = sha256(filename.encode("utf-8") + b"\0" + document).hexdigest()

        async def build() -> tuple[str, SearchIndex]:
            # PDF parsing can be CPU-heavy, so keep it off the API event loop.
            document_type, passages = await asyncio.to_thread(
                parse_document, document, filename, self.settings
            )
            embeddings = await self.provider.embed([passage.text for passage in passages])
            return document_type, SearchIndex.build(passages, embeddings)

        return await self.cache.get_or_build(cache_key, build)

    async def answer(
        self, document: bytes, filename: str, questions: list[Question]
    ) -> tuple[DocumentSummary, list[AnswerResult]]:
        document_type, index = await self._index(document, filename)
        unique_questions: list[Question] = []
        seen_text: set[str] = set()
        # Repeated question text needs one model call; results are mapped back to every
        # original ID and position below.
        for question in questions:
            if question.text not in seen_text:
                unique_questions.append(question)
                seen_text.add(question.text)
        question_vectors = await self.provider.embed(
            [question.text for question in unique_questions]
        )

        async def one(question: Question, vector: list[float]) -> AnswerResult:
            # Ranking can scan hundreds of vectors per question; run that CPU work
            # outside the event loop while other requests await model I/O.
            passages = await asyncio.to_thread(
                index.search, question.text, vector, self.settings.retrieval_top_k
            )
            async with self.answer_semaphore:
                draft = await self.provider.answer(question.text, passages)
            # Citation IDs from the model are untrusted until matched to retrieved evidence.
            passage_by_id = {passage.id: passage for passage in passages}
            unique_ids = list(dict.fromkeys(draft.citation_ids))
            valid_ids = [passage_id for passage_id in unique_ids if passage_id in passage_by_id]
            # An answer without a valid supporting passage cannot be reported as grounded.
            if (
                not draft.is_answerable
                or not draft.answer
                or draft.answer.strip().lower() == "data-not-found"
                or not valid_ids
            ):
                return AnswerResult(
                    id=question.id,
                    question=question.text,
                    answer="Data-Not-Found",
                    comments=(draft.comments or "No supporting passage was found in the document.")[
                        :2_000
                    ],
                    confidence="low",
                    status="not_found",
                    citations=[],
                )
            citations = [
                Citation(
                    passage_id=passage_id,
                    source=passage_by_id[passage_id].source,
                    page=passage_by_id[passage_id].page,
                    json_path=passage_by_id[passage_id].json_path,
                    excerpt=passage_by_id[passage_id].text,
                )
                for passage_id in valid_ids
            ]
            return AnswerResult(
                id=question.id,
                question=question.text,
                answer=draft.answer[:2_000],
                comments=draft.comments[:2_000],
                confidence=draft.evidence_strength,
                status="answered",
                citations=citations,
            )

        unique_results = await asyncio.gather(
            *(
                one(question, vector)
                for question, vector in zip(unique_questions, question_vectors, strict=True)
            )
        )
        results_by_text = {result.question: result for result in unique_results}
        results = [
            results_by_text[question.text].model_copy(update={"id": question.id})
            for question in questions
        ]
        return (
            DocumentSummary(name=filename, type=document_type, passages=len(index.passages)),
            results,
        )

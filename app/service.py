"""Orchestrate extraction, indexing, retrieval, and bounded answer calls."""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from hashlib import sha256

from app.config import Settings
from app.errors import ServiceError
from app.llm import ModelProvider
from app.observability import current_metrics, measure
from app.parsing import parse_document
from app.resources import BlockingWorkPool
from app.retrieval import SearchIndex, validate_embeddings
from app.schemas import (
    AnswerResult,
    Citation,
    DocumentSummary,
    DraftAnswer,
    Passage,
    Question,
    ResultError,
    StructuredAnswer,
)
from app.workflow import HybridRetriever, build_qa_chain

logger = logging.getLogger("document_rag")


@dataclass
class _IndexBuild:
    task: asyncio.Task[tuple[str, SearchIndex]]
    waiters: int = 0


class IndexCache:
    """Bounded, process-local LRU cache for parsed and embedded documents."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._items: OrderedDict[str, tuple[float, str, SearchIndex]] = OrderedDict()
        self._inflight: dict[str, _IndexBuild] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._clock = time.monotonic

    async def get_or_build(
        self,
        key: str,
        builder: Callable[[], Awaitable[tuple[str, SearchIndex]]],
    ) -> tuple[str, SearchIndex]:
        """Share one parse and embedding task across simultaneous identical uploads."""
        async with self._lock:
            if self._closed:
                raise ServiceError(503, "service_stopping", "The service is shutting down.")
            now = self._clock()
            for expired in [item for item, value in self._items.items() if value[0] <= now]:
                del self._items[expired]
            metrics = current_metrics.get()
            entry = self._items.get(key)
            if entry is not None:
                expires_at, document_type, index = entry
                if expires_at > self._clock():
                    self._items.move_to_end(key)
                    if metrics is not None:
                        metrics.cache_status = "hit"
                    return document_type, index
                del self._items[key]
            build = self._inflight.get(key)
            if build is None:
                if len(self._inflight) >= self.settings.max_index_builds:
                    raise ServiceError(
                        503, "index_busy", "Document processing is busy. Try again shortly."
                    )
                task = asyncio.create_task(self._build(key, builder))
                task.add_done_callback(self._observe_build)
                build = _IndexBuild(task)
                self._inflight[key] = build
                if metrics is not None:
                    metrics.cache_status = "miss"
            elif build.task.cancelling():
                raise ServiceError(
                    503, "index_busy", "Document processing is stopping. Try again shortly."
                )
            elif metrics is not None:
                metrics.cache_status = "shared"
            build.waiters += 1
        try:
            # Cancellation of one caller must not interrupt another caller's build.
            return await asyncio.shield(build.task)
        finally:
            async with self._lock:
                if self._inflight.get(key) is build:
                    build.waiters -= 1
                    if build.waiters == 0 and not build.task.done():
                        build.task.cancel()

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
            try:
                async with asyncio.timeout(self.settings.index_timeout_seconds):
                    document_type, index = await builder()
            except TimeoutError as exc:
                raise ServiceError(
                    504, "index_timeout", "Document indexing took too long."
                ) from exc
            await self.put(key, document_type, index)
            return document_type, index
        finally:
            async with self._lock:
                build = self._inflight.get(key)
                if build is not None and build.task is asyncio.current_task():
                    self._inflight.pop(key, None)

    async def put(self, key: str, document_type: str, index: SearchIndex) -> None:
        async with self._lock:
            if self._closed:
                return
            self._items[key] = (
                self._clock() + self.settings.cache_ttl_seconds,
                document_type,
                index,
            )
            self._items.move_to_end(key)
            while len(self._items) > self.settings.cache_entries:
                self._items.popitem(last=False)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = [build.task for build in self._inflight.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._items.clear()


class DocumentQAService:
    """Answer uploaded questions while enforcing retrieval and citation boundaries."""

    def __init__(
        self,
        provider: ModelProvider,
        settings: Settings,
        cache: IndexCache | None = None,
        answer_semaphore: asyncio.Semaphore | None = None,
        work_pool: BlockingWorkPool | None = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.cache = cache or IndexCache(settings)
        self.answer_semaphore = answer_semaphore or asyncio.Semaphore(
            settings.max_concurrent_answers
        )
        self.work_pool = work_pool

    async def _blocking(self, function, *args):
        if self.work_pool is not None:
            return await self.work_pool.run(function, *args)
        return await asyncio.to_thread(function, *args)

    async def _index(self, document: bytes, filename: str) -> tuple[str, SearchIndex]:
        # The filename affects citation metadata, so identical bytes under another name
        # need a separate cached index.
        cache_key = sha256(filename.encode("utf-8") + b"\0" + document).hexdigest()

        async def build() -> tuple[str, SearchIndex]:
            # PDF parsing can be CPU-heavy, so keep it off the API event loop.
            with measure("extraction"):
                document_type, passages = await self._blocking(
                    parse_document, document, filename, self.settings
                )
            embeddings = await self.provider.embed([passage.text for passage in passages])
            try:
                with measure("index_build"):
                    index = await self._blocking(SearchIndex.build, passages, embeddings)
                return document_type, index
            except ValueError as exc:
                raise ServiceError(
                    502, "invalid_embedding_response", "Embedding service returned invalid data."
                ) from exc

        with measure("index_wait"):
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
        try:
            validate_embeddings(question_vectors, len(unique_questions), len(index.vectors[0]))
        except ValueError as exc:
            raise ServiceError(
                502, "invalid_embedding_response", "Embedding service returned invalid data."
            ) from exc
        retriever = HybridRetriever(
            index=index,
            question_vectors=dict(
                zip([question.text for question in unique_questions], question_vectors, strict=True)
            ),
            top_k=self.settings.retrieval_top_k,
            work_pool=self.work_pool,
        )
        qa_chain = build_qa_chain(retriever, self.provider, self.answer_semaphore)

        async def one(question: Question) -> AnswerResult:
            try:
                # This deadline includes retrieval and waiting for the model slot.
                # A question timeout preserves other results; request-wide deadlines
                # and document indexing failures still fail the HTTP request.
                async with asyncio.timeout(self.settings.question_timeout_seconds):
                    draft, passages = await qa_chain.ainvoke(question.text)
                return self._ground_result(question, draft, passages)
            except TimeoutError:
                return self._error_result(question, "question_timeout", "This question timed out.")
            except ServiceError as exc:
                return self._error_result(question, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - isolate unexpected per-question failures
                # Do not expose raw provider text or documents in unexpected failures.
                logger.warning("question_failed", extra={"error_type": type(exc).__name__})
                return self._error_result(
                    question, "question_processing_failed", "Could not process this question."
                )

        unique_results = await asyncio.gather(*(one(question) for question in unique_questions))
        results_by_text = {result.question: result for result in unique_results}
        results = [
            results_by_text[question.text].model_copy(update={"id": question.id})
            for question in questions
        ]
        return (
            DocumentSummary(name=filename, type=document_type, passages=len(index.passages)),
            results,
        )

    @staticmethod
    def _error_result(question: Question, code: str, message: str) -> AnswerResult:
        return AnswerResult(
            id=question.id,
            question=question.text,
            answer=None,
            comments="",
            confidence=None,
            status="error",
            citations=[],
            error=ResultError(code=code, message=message),
        )

    @staticmethod
    def _ground_result(
        question: Question, draft: DraftAnswer, passages: list[Passage]
    ) -> AnswerResult:
        # Validate injected providers too, so the service contract does not depend
        # on which model adapter is used. Overlong answers are never silently cut.
        try:
            draft = StructuredAnswer.model_validate(asdict(draft)).to_draft()
        except (ValueError, TypeError) as exc:
            raise ServiceError(
                502, "invalid_model_response", "Answer service returned invalid data."
            ) from exc
        if not draft.is_answerable:
            return AnswerResult(
                id=question.id,
                question=question.text,
                answer="Data-Not-Found",
                comments=draft.comments or "No supporting passage was found in the document.",
                confidence="low",
                status="not_found",
                citations=[],
            )
        passage_by_id = {passage.id: passage for passage in passages}
        unique_ids = list(dict.fromkeys(draft.citation_ids))
        if any(passage_id not in passage_by_id for passage_id in unique_ids):
            # A fabricated citation is a model failure, not proof that the source
            # lacks an answer. Reject the complete answer if any citation is invalid.
            raise ServiceError(
                502, "invalid_model_citation", "The model returned an invalid source citation."
            )
        quotes = {item["passage_id"]: item["quote"] for item in draft.evidence_quotes}
        if len(quotes) != len(draft.evidence_quotes) or set(quotes) != set(unique_ids):
            raise ServiceError(
                502, "invalid_model_quote", "Each source citation needs a supporting quote."
            )
        for passage_id, quote in quotes.items():
            normalized_quote = " ".join(quote.split())
            if not normalized_quote or normalized_quote not in " ".join(
                passage_by_id[passage_id].text.split()
            ):
                raise ServiceError(
                    502, "invalid_model_quote", "A supporting quote does not match the source."
                )
        citations = [
            Citation(
                passage_id=passage_id,
                source=passage_by_id[passage_id].source,
                page=passage_by_id[passage_id].page,
                json_path=passage_by_id[passage_id].json_path,
                excerpt=passage_by_id[passage_id].text,
                quote=quotes[passage_id],
            )
            for passage_id in unique_ids
        ]
        return AnswerResult(
            id=question.id,
            question=question.text,
            answer=draft.answer,
            comments=draft.comments,
            confidence=draft.evidence_strength,
            status="answered",
            citations=citations,
        )

"""LangChain retrieve-then-generate workflow with citable document metadata."""

import asyncio
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable, RunnableLambda, RunnableParallel, RunnablePassthrough

from app.llm import ModelProvider
from app.observability import measure
from app.resources import BlockingWorkPool
from app.retrieval import SearchIndex
from app.schemas import DraftAnswer, Passage


class HybridRetriever(BaseRetriever):
    """Adapt hybrid ranking to LangChain while reusing batched question embeddings."""

    index: SearchIndex
    question_vectors: dict[str, list[float]]
    top_k: int
    work_pool: BlockingWorkPool | None = None

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        # The async adapter below uses the application's bounded pool; standalone
        # callers fall back to BaseRetriever's executor. Neither blocks the event loop.
        with measure("retrieval"):
            passages = self.index.search(query, self.question_vectors[query], self.top_k)
        return [passage.to_document() for passage in passages]

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        if self.work_pool is not None:
            return await self.work_pool.run(
                self._get_relevant_documents, query, run_manager=run_manager.get_sync()
            )
        return await super()._aget_relevant_documents(query, run_manager=run_manager)


def build_qa_chain(
    retriever: HybridRetriever, provider: ModelProvider, semaphore: asyncio.Semaphore
) -> Runnable[str, tuple[DraftAnswer, list[Passage]]]:
    async def generate(inputs: dict[str, Any]) -> tuple[DraftAnswer, list[Passage]]:
        passages = [Passage.from_document(document) for document in inputs["documents"]]
        async with semaphore:
            draft = await provider.answer(inputs["question"], passages)
        return draft, passages

    # LangChain composes retrieval and generation. The provider composes its
    # prompt and structured chat model; the service validates citations afterward.
    return RunnableParallel(question=RunnablePassthrough(), documents=retriever) | RunnableLambda(
        generate
    )

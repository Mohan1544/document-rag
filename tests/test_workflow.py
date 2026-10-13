import asyncio

import pytest
from langchain_core.documents import Document
from langchain_core.runnables import RunnableSequence

from app.retrieval import SearchIndex
from app.schemas import Passage
from app.workflow import HybridRetriever, build_qa_chain
from tests.conftest import FakeProvider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "page", "json_path"),
    [
        ("report.pdf", 16, None),
        ("source.json", None, "$.hosting"),
    ],
)
async def test_langchain_workflow_preserves_evidence_locations(source, page, json_path) -> None:
    passage = Passage("p1", "The service runs on GCP.", source, page=page, json_path=json_path)
    retriever = HybridRetriever(
        index=SearchIndex.build([passage], [[1.0, 1.0]]),
        question_vectors={"Which cloud?": [1.0, 1.0]},
        top_k=1,
    )
    documents = await retriever.ainvoke("Which cloud?")
    assert isinstance(documents[0], Document)
    assert documents[0].metadata["page"] == page
    assert documents[0].metadata["json_path"] == json_path
    chain = build_qa_chain(retriever, FakeProvider(), asyncio.Semaphore(1))
    assert isinstance(chain, RunnableSequence)
    draft, passages = await chain.ainvoke("Which cloud?")
    assert passages == [passage]
    assert draft.citation_ids == [passage.id]

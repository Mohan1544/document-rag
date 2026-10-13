"""Transport and internal data models."""

from dataclasses import dataclass, field
from typing import Literal

from langchain_core.documents import Document
from pydantic import BaseModel, ConfigDict, Field, model_validator


@dataclass(frozen=True)
class Question:
    id: str
    text: str


@dataclass(frozen=True)
class Passage:
    id: str
    text: str
    source: str
    page: int | None = None
    json_path: str | None = None

    def to_document(self) -> Document:
        """Carry citation locations through LangChain retrieval without losing IDs."""
        return Document(
            id=self.id,
            page_content=self.text,
            metadata={
                "passage_id": self.id,
                "source": self.source,
                "page": self.page,
                "json_path": self.json_path,
            },
        )

    @classmethod
    def from_document(cls, document: Document) -> "Passage":
        return cls(
            id=document.metadata["passage_id"],
            text=document.page_content,
            source=document.metadata["source"],
            page=document.metadata.get("page"),
            json_path=document.metadata.get("json_path"),
        )


class Citation(BaseModel):
    passage_id: str = Field(max_length=80)
    source: str = Field(max_length=120)
    page: int | None = None
    json_path: str | None = Field(default=None, max_length=500)
    excerpt: str = Field(max_length=1_500)
    quote: str | None = Field(default=None, max_length=350)


class ResultError(BaseModel):
    code: str = Field(max_length=80)
    message: str = Field(max_length=500)


class AnswerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=100)
    question: str = Field(max_length=500)
    answer: str | None = Field(max_length=2_000)
    comments: str = Field(max_length=2_000)
    confidence: Literal["high", "medium", "low"] | None
    status: Literal["answered", "not_found", "error"]
    citations: list[Citation]
    error: ResultError | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "AnswerResult":
        """Make processing failures distinguishable from unsupported answers."""
        if self.status == "error":
            if self.error is None or self.answer is not None or self.confidence is not None:
                raise ValueError("An error result needs an error and no answer or confidence")
            if self.citations:
                raise ValueError("An error result cannot have citations")
        elif self.status == "not_found":
            if (
                self.answer != "Data-Not-Found"
                or self.confidence != "low"
                or self.citations
                or self.error is not None
            ):
                raise ValueError("A not_found result must have no citations or processing error")
        elif (
            not self.answer
            or not self.answer.strip()
            or self.answer.strip().lower() == "data-not-found"
            or self.confidence is None
            or not self.citations
            or self.error is not None
        ):
            raise ValueError("An answered result needs an answer, confidence, and citations")
        return self


class DocumentSummary(BaseModel):
    name: str
    type: Literal["pdf", "json"]
    passages: int


class AnswerResponse(BaseModel):
    request_id: str
    document: DocumentSummary
    results: list[AnswerResult]


class ExportRequest(BaseModel):
    results: list[AnswerResult] = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class DraftAnswer:
    answer: str
    comments: str
    citation_ids: list[str]
    is_answerable: bool
    evidence_strength: Literal["high", "medium", "low"]
    evidence_quotes: list[dict[str, str]] = field(default_factory=list)


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    passage_id: str = Field(max_length=80)
    quote: str = Field(min_length=1, max_length=350)


class StructuredAnswer(BaseModel):
    """Single schema for model output and local validation before publishing it."""

    model_config = ConfigDict(strict=True, extra="forbid")

    answer: str = Field(max_length=2_000)
    comments: str = Field(max_length=2_000)
    citation_ids: list[str]
    is_answerable: bool
    evidence_strength: Literal["high", "medium", "low"]
    evidence_quotes: list[EvidenceQuote]

    @model_validator(mode="after")
    def validate_answerability(self) -> "StructuredAnswer":
        if self.is_answerable and (
            not self.answer.strip()
            or self.answer.strip().lower() == "data-not-found"
            or not self.citation_ids
        ):
            raise ValueError("An answerable result needs answer text and citation IDs")
        return self

    def to_draft(self) -> DraftAnswer:
        return DraftAnswer(
            answer=self.answer.strip(),
            comments=self.comments.strip(),
            citation_ids=self.citation_ids,
            is_answerable=self.is_answerable,
            evidence_strength=self.evidence_strength,
            evidence_quotes=[evidence.model_dump() for evidence in self.evidence_quotes],
        )

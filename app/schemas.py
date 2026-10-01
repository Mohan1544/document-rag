"""Transport and internal data models."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


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


class Citation(BaseModel):
    passage_id: str = Field(max_length=80)
    source: str = Field(max_length=120)
    page: int | None = None
    json_path: str | None = Field(default=None, max_length=500)
    excerpt: str = Field(max_length=1_500)


class AnswerResult(BaseModel):
    id: str = Field(max_length=100)
    question: str = Field(max_length=500)
    answer: str = Field(max_length=2_000)
    comments: str = Field(max_length=2_000)
    confidence: Literal["high", "medium", "low"]
    status: Literal["answered", "not_found"]
    citations: list[Citation]


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

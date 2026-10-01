"""Read untrusted upload bytes into bounded, citable passages."""

import json
import re
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from app.config import Settings
from app.errors import ServiceError
from app.schemas import Passage, Question


def safe_filename(filename: str | None) -> str:
    name = (filename or "document").replace("\\", "/").split("/")[-1]
    return re.sub(r"[^\w.() -]", "_", name)[:120] or "document"


def parse_questions(data: bytes, settings: Settings) -> list[Question]:
    """Accept string or object questions and preserve caller IDs and order."""
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError(
            422, "invalid_questions_json", "Questions must be valid UTF-8 JSON."
        ) from exc

    entries = payload.get("questions") if isinstance(payload, dict) else payload
    if not isinstance(entries, list) or not entries:
        raise ServiceError(422, "invalid_questions", "Provide a non-empty JSON list of questions.")
    if len(entries) > settings.max_questions:
        raise ServiceError(
            413, "too_many_questions", f"At most {settings.max_questions} questions are allowed."
        )

    questions: list[Question] = []
    ids: set[str] = set()
    for number, entry in enumerate(entries, start=1):
        if isinstance(entry, str):
            question_id, question_text = str(number), entry
        elif isinstance(entry, dict):
            question_id = entry.get("id", str(number))
            question_text = entry.get("question")
        else:
            raise ServiceError(
                422, "invalid_question", f"Question {number} must be text or an object."
            )
        if not isinstance(question_id, (str, int)) or not str(question_id).strip():
            raise ServiceError(422, "invalid_question_id", f"Question {number} has an invalid id.")
        question_id = str(question_id).strip()
        if len(question_id) > 100:
            raise ServiceError(
                413, "question_id_too_long", f"Question {number} id exceeds 100 characters."
            )
        if question_id in ids:
            raise ServiceError(422, "duplicate_question_id", "Question ids must be unique.")
        if not isinstance(question_text, str) or not question_text.strip():
            raise ServiceError(422, "empty_question", f"Question {number} is empty.")
        question_text = question_text.strip()
        if len(question_text) > settings.max_question_chars:
            raise ServiceError(
                413,
                "question_too_long",
                f"Question {number} exceeds {settings.max_question_chars} characters.",
            )
        questions.append(Question(id=question_id, text=question_text))
        ids.add(question_id)
    return questions


def _splitter(settings: Settings) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )


def _clean_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\x00", "")).strip()


def parse_pdf(data: bytes, filename: str, settings: Settings) -> list[Passage]:
    """Extract text page by page so every passage has a usable PDF citation."""
    if not data.startswith(b"%PDF-"):
        raise ServiceError(422, "invalid_pdf", "The uploaded PDF has an invalid file signature.")
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ServiceError(422, "encrypted_pdf", "Password-protected PDFs are not supported.")
        if len(reader.pages) > settings.max_pdf_pages:
            raise ServiceError(
                413, "too_many_pages", f"PDFs may contain at most {settings.max_pdf_pages} pages."
            )
        passages: list[Passage] = []
        total_chars = 0
        splitter = _splitter(settings)
        for page_number, page in enumerate(reader.pages, start=1):
            text = _clean_text(page.extract_text() or "")
            total_chars += len(text)
            if total_chars > settings.max_extracted_chars:
                raise ServiceError(
                    413, "document_too_large", "Extracted document text is too large."
                )
            for chunk_number, chunk in enumerate(splitter.split_text(text), start=1):
                passages.append(
                    Passage(
                        id=f"p{page_number}-c{chunk_number}",
                        text=chunk,
                        source=filename,
                        page=page_number,
                    )
                )
                if len(passages) > settings.max_chunks:
                    raise ServiceError(413, "too_many_passages", "Document has too many passages.")
    except ServiceError:
        raise
    except Exception as exc:
        raise ServiceError(422, "invalid_pdf", "Could not read the PDF document.") from exc
    if not passages:
        raise ServiceError(
            422,
            "no_extractable_text",
            "The PDF contains no extractable text. Scanned PDFs need OCR before upload.",
        )
    return passages


def _json_path(parent: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key, ensure_ascii=False)}]"


def _flatten_scalar_lines(value: Any, path: str, depth: int = 0) -> Iterator[str]:
    if depth > 30:
        raise ServiceError(422, "json_too_deep", "Source JSON is nested too deeply.")
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _flatten_scalar_lines(child, _json_path(path, str(key)), depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _flatten_scalar_lines(child, _json_path(path, index), depth + 1)
    elif value is not None:
        yield f"{path}: {value}"


def _json_records(value: Any, path: str = "$", depth: int = 0) -> Iterator[tuple[str, str]]:
    if depth > 30:
        raise ServiceError(422, "json_too_deep", "Source JSON is nested too deeply.")
    if isinstance(value, list):
        if all(not isinstance(item, (dict, list)) for item in value):
            yield path, "\n".join(_flatten_scalar_lines(value, path))
        else:
            for index, child in enumerate(value):
                yield from _json_records(child, _json_path(path, index), depth + 1)
    elif isinstance(value, dict):
        if not value:
            return
        # Keep Q&A entries together so retrieval does not separate an answer from its
        # question. Other nested objects retain their own JSON paths for citations.
        if ("question" in value and "answer" in value) or all(
            not isinstance(child, (dict, list)) for child in value.values()
        ):
            yield path, "\n".join(_flatten_scalar_lines(value, path))
        else:
            for key, child in value.items():
                yield from _json_records(child, _json_path(path, str(key)), depth + 1)
    elif value is not None:
        yield path, f"{path}: {value}"


def parse_json_document(data: bytes, filename: str, settings: Settings) -> list[Passage]:
    """Split JSON records while carrying their paths into citation metadata."""
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError(422, "invalid_source_json", "Source must be valid UTF-8 JSON.") from exc

    passages: list[Passage] = []
    total_chars = 0
    splitter = _splitter(settings)
    for path, record in _json_records(payload):
        # Citation schemas cap JSON paths; reject an oversized key path here so
        # a valid-looking upload cannot fail later during response serialization.
        if len(path) > 500:
            raise ServiceError(413, "json_path_too_long", "Source JSON key path is too long.")
        record = _clean_text(record)
        total_chars += len(record)
        if total_chars > settings.max_extracted_chars:
            raise ServiceError(413, "document_too_large", "Source JSON contains too much text.")
        for chunk_number, chunk in enumerate(splitter.split_text(record), start=1):
            passages.append(
                Passage(
                    id=f"j{len(passages) + 1}",
                    text=chunk,
                    source=filename,
                    json_path=path,
                )
            )
            if len(passages) > settings.max_chunks:
                raise ServiceError(413, "too_many_passages", "Document has too many passages.")
    if not passages:
        raise ServiceError(422, "empty_source", "Source JSON contains no usable text.")
    return passages


def parse_document(data: bytes, filename: str, settings: Settings) -> tuple[str, list[Passage]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "pdf", parse_pdf(data, filename, settings)
    if suffix == ".json":
        return "json", parse_json_document(data, filename, settings)
    raise ServiceError(415, "unsupported_source", "Document must be a .pdf or .json file.")

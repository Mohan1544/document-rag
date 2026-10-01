"""Validated configuration with conservative defaults for uploaded documents."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    max_document_bytes: int = 10_000_000
    max_questions_bytes: int = 100_000
    max_questions: int = 50
    max_question_chars: int = 500
    max_pdf_pages: int = 150
    max_extracted_chars: int = 500_000
    max_chunks: int = 500
    max_concurrent_answers: int = 4
    request_timeout_seconds: int = 120
    openai_timeout_seconds: int = 30
    retrieval_top_k: int = 5
    chunk_size: int = 1_400
    chunk_overlap: int = 200
    cache_entries: int = 4
    cache_ttl_seconds: int = 900

    @classmethod
    def from_env(cls) -> "Settings":
        fields = cls.__dataclass_fields__
        values = {}
        for name in fields:
            env_name = name.upper()
            raw = os.getenv(env_name)
            if raw is not None:
                value = int(raw)
                if value <= 0:
                    raise ValueError(f"{env_name} must be positive")
                values[name] = value
        settings = cls(**values)
        if settings.chunk_overlap >= settings.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        # Response schemas cap question text and citation excerpts. Keep environment
        # overrides within those bounds so valid uploads cannot fail on output.
        if settings.max_question_chars > 500:
            raise ValueError("MAX_QUESTION_CHARS must be at most 500")
        if settings.chunk_size > 1_500:
            raise ValueError("CHUNK_SIZE must be at most 1500")
        return settings

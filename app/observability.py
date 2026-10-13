"""Request-local measurements shared by async tasks and worker threads."""

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class RequestMetrics:
    request_id: str
    cache_status: str = "none"
    counters: dict[str, int] = field(default_factory=dict)
    stages: dict[str, float] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def add(self, **values: int) -> None:
        with self._lock:
            for name, value in values.items():
                self.counters[name] = self.counters.get(name, 0) + value

    def stage(self, name: str, milliseconds: float) -> None:
        with self._lock:
            self.stages[name] = self.stages.get(name, 0.0) + milliseconds

    def summary(self) -> dict:
        with self._lock:
            counters = {
                name: self.counters.get(name, 0)
                for name in (
                    "embedding_calls",
                    "answer_calls",
                    "embedding_tokens",
                    "prompt_tokens",
                    "completion_tokens",
                    "answered",
                    "not_found",
                    "errors",
                )
            }
            return {
                "cache_status": self.cache_status,
                **counters,
                "model_calls": counters["embedding_calls"] + counters["answer_calls"],
                "total_tokens": counters["embedding_tokens"]
                + counters["prompt_tokens"]
                + counters["completion_tokens"],
                "stage_ms": {key: round(value, 1) for key, value in self.stages.items()},
            }


current_metrics: ContextVar[RequestMetrics | None] = ContextVar("request_metrics", default=None)


def count(**values: int) -> None:
    metrics = current_metrics.get()
    if metrics is not None:
        metrics.add(**values)


@contextmanager
def measure(stage: str):
    started = time.monotonic()
    try:
        yield
    finally:
        metrics = current_metrics.get()
        if metrics is not None:
            metrics.stage(stage, (time.monotonic() - started) * 1000)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        metrics = current_metrics.get()
        if metrics is not None:
            payload["request_id"] = metrics.request_id
        # Explicitly whitelist metadata; never serialize messages, prompts, keys,
        # uploaded content, or raw exception bodies from arbitrary LogRecord fields.
        for name in (
            "request_id",
            "path",
            "status",
            "duration_ms",
            "questions",
            "document_type",
            "tokens",
            "prompt_tokens",
            "completion_tokens",
            "upstream_code",
            "upstream_param",
            "upstream_message",
            "api_host",
            "upstream_request_id",
            "upstream_content_type",
            "upstream_has_body",
            "batch_start",
            "error_type",
            "summary",
            "passages",
        ):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    logger = logging.getLogger("document_rag")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

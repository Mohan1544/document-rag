"""FastAPI entry point and reviewer-facing upload UI."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.errors import ServiceError
from app.export import build_workbook
from app.llm import ModelProvider, OpenAIProvider
from app.parsing import parse_questions, safe_filename
from app.schemas import AnswerResponse, ExportRequest
from app.service import DocumentQAService, IndexCache

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger("document_rag")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for field in (
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
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


async def _read_bounded(upload: UploadFile, limit: int, label: str) -> bytes:
    """Read one byte past the limit so oversized uploads fail deterministically."""
    data = await upload.read(limit + 1)
    await upload.close()
    if len(data) > limit:
        raise ServiceError(
            413, f"{label}_too_large", f"{label.title()} file exceeds {limit} bytes."
        )
    if not data:
        raise ServiceError(422, f"empty_{label}", f"{label.title()} file is empty.")
    return data


def create_app(settings: Settings | None = None, provider: ModelProvider | None = None) -> FastAPI:
    """Build the API; injected providers let tests run without external model calls."""
    configure_logging()
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        runtime_provider = application.state.provider
        if isinstance(runtime_provider, OpenAIProvider):
            await runtime_provider.client.close()

    app = FastAPI(title="Document RAG API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.provider = provider
    app.state.provider_lock = asyncio.Lock()
    # Share the cache and answer limit across requests in this process.
    app.state.cache = IndexCache(settings)
    app.state.answer_semaphore = asyncio.Semaphore(settings.max_concurrent_answers)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def request_logging(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = uuid4().hex
        request.state.request_id = request_id
        started = time.monotonic()
        content_length = request.headers.get("content-length")
        # Reject obvious oversized bodies before multipart parsing; _read_bounded
        # enforces the actual file limits even when Content-Length is absent.
        if content_length and request.url.path in {"/api/v1/answer", "/api/v1/export.xlsx"}:
            try:
                limit = (
                    settings.max_document_bytes + settings.max_questions_bytes + 100_000
                    if request.url.path == "/api/v1/answer"
                    else 3_000_000
                )
                if int(content_length) > limit:
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "request_too_large",
                                "message": "Request is too large.",
                            }
                        },
                    )
                    response.headers["X-Request-ID"] = request_id
                    logger.info(
                        "http_request",
                        extra={
                            "request_id": request_id,
                            "path": request.url.path,
                            "status": response.status_code,
                            "duration_ms": round((time.monotonic() - started) * 1_000, 1),
                        },
                    )
                    return response
            except ValueError:
                pass
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "http_request",
            extra={
                "request_id": request_id,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1_000, 1),
            },
        )
        return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/answer", response_model=AnswerResponse)
    async def answer(
        request: Request,
        questions_file: Annotated[UploadFile, File()],
        document_file: Annotated[UploadFile, File()],
    ) -> AnswerResponse:
        if not (questions_file.filename or "").lower().endswith(".json"):
            raise ServiceError(415, "unsupported_questions", "Questions file must be .json.")
        filename = safe_filename(document_file.filename)
        if Path(filename).suffix.lower() not in {".pdf", ".json"}:
            raise ServiceError(415, "unsupported_source", "Document must be a .pdf or .json file.")
        question_data, document_data = await asyncio.gather(
            _read_bounded(questions_file, settings.max_questions_bytes, "questions"),
            _read_bounded(document_file, settings.max_document_bytes, "document"),
        )
        questions = parse_questions(question_data, settings)
        # Create the client on the first answer request so health checks and offline
        # tests work without an API key; lifespan closes the shared client.
        async with app.state.provider_lock:
            if app.state.provider is None:
                app.state.provider = OpenAIProvider(settings)
            active_provider = app.state.provider
        service = DocumentQAService(
            active_provider, settings, app.state.cache, app.state.answer_semaphore
        )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                summary, results = await service.answer(document_data, filename, questions)
        except TimeoutError as exc:
            raise ServiceError(
                504, "request_timeout", "The document took too long to process."
            ) from exc
        logger.info(
            "answer_completed",
            extra={
                "request_id": request.state.request_id,
                "questions": len(questions),
                "document_type": summary.type,
            },
        )
        return AnswerResponse(
            request_id=request.state.request_id, document=summary, results=results
        )

    @app.post("/api/v1/export.xlsx", include_in_schema=True)
    async def export_excel(payload: ExportRequest) -> Response:
        content = await asyncio.to_thread(build_workbook, payload.results)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="answers.xlsx"'},
        )

    return app


app = create_app()

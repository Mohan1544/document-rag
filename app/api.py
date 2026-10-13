"""FastAPI entry point and reviewer-facing upload UI."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from app.config import Settings
from app.errors import ServiceError
from app.export import build_workbook
from app.llm import ModelProvider, OpenAIProvider
from app.middleware import LimitedUploadRoute, RequestBoundaryMiddleware, error_response
from app.observability import configure_logging, current_metrics
from app.parsing import parse_questions, safe_filename
from app.resources import BlockingWorkPool
from app.schemas import AnswerResponse, ExportRequest
from app.service import DocumentQAService, IndexCache

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger("document_rag")


async def _read_bounded(upload: UploadFile, limit: int, label: str) -> bytes:
    """Read one byte past the limit so oversized uploads fail deterministically."""
    try:
        data = await upload.read(limit + 1)
    finally:
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
        try:
            yield
        finally:
            # Stop shared tasks before closing model clients they might still use.
            await application.state.cache.aclose()
            await application.state.work_pool.aclose()
            runtime_provider = application.state.provider
            if isinstance(runtime_provider, OpenAIProvider):
                await runtime_provider.aclose()

    app = FastAPI(title="Document RAG API", version="0.1.0", lifespan=lifespan)
    app.router.route_class = LimitedUploadRoute
    app.state.settings = settings
    app.state.provider = provider
    app.state.provider_lock = asyncio.Lock()
    # Share the cache and answer limit across requests in this process.
    app.state.cache = IndexCache(settings)
    app.state.answer_semaphore = asyncio.Semaphore(settings.max_concurrent_answers)
    app.state.work_pool = BlockingWorkPool(settings.blocking_workers)
    app.add_middleware(RequestBoundaryMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            422,
            "validation_error",
            "Invalid request. Check required files and field formats.",
            request.state.request_id,
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        messages = {
            400: ("invalid_request", "Could not read the request. Upload two valid files."),
            404: ("route_not_found", "The requested endpoint does not exist."),
            405: ("method_not_allowed", "This endpoint does not support that method."),
            413: ("request_too_large", "Request is too large."),
        }
        code, message = messages.get(
            exc.status_code, ("invalid_request", "The request was rejected.")
        )
        return error_response(exc.status_code, code, message, request.state.request_id)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready(request: Request) -> dict[str, str]:
        if app.state.provider is None and not os.getenv("OPENAI_API_KEY", "").strip():
            raise ServiceError(503, "missing_api_key", "OPENAI_API_KEY is not configured.")
        return {"status": "ready"}

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
        question_data = await _read_bounded(
            questions_file, settings.max_questions_bytes, "questions"
        )
        document_data = await _read_bounded(document_file, settings.max_document_bytes, "document")
        questions = parse_questions(question_data, settings)
        # Create the client on the first answer request so health checks and offline
        # tests work without an API key; lifespan closes the shared client.
        async with app.state.provider_lock:
            if app.state.provider is None:
                app.state.provider = OpenAIProvider(settings)
            active_provider = app.state.provider
        service = DocumentQAService(
            active_provider,
            settings,
            app.state.cache,
            app.state.answer_semaphore,
            app.state.work_pool,
        )
        summary, results = await service.answer(document_data, filename, questions)
        metrics = current_metrics.get()
        if metrics is not None:
            metrics.add(
                answered=sum(result.status == "answered" for result in results),
                not_found=sum(result.status == "not_found" for result in results),
                errors=sum(result.status == "error" for result in results),
            )
        logger.info(
            "answer_completed",
            extra={
                "request_id": request.state.request_id,
                "questions": len(questions),
                "document_type": summary.type,
                "passages": summary.passages,
                "summary": metrics.summary() if metrics is not None else {},
            },
        )
        return AnswerResponse(
            request_id=request.state.request_id, document=summary, results=results
        )

    @app.post("/api/v1/export.xlsx", include_in_schema=True)
    async def export_excel(payload: ExportRequest) -> Response:
        content = await app.state.work_pool.run(build_workbook, payload.results)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="answers.xlsx"'},
        )

    return app


app = create_app()

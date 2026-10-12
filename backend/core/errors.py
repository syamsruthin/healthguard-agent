"""Application error types and uniform JSON error responses."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.core.logging import get_logger

log = get_logger(__name__)


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.headers = headers or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class AuthError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"


def _payload(code: str, message: str, request: Request) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.code, exc.message, request),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        payload = _payload("validation_error", "Request validation failed", request)
        payload["error"]["details"] = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content=payload)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path)
        return JSONResponse(
            status_code=500, content=_payload("internal_error", "Internal server error", request)
        )

from collections.abc import Mapping
from typing import Any

from fastapi import Request
from starlette.exceptions import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog

from app.core.safety import redact_error_details, redact_sensitive_text


class ApplicationError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def request_id_for(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def error_payload(
    request: Request,
    code: str,
    message: str,
    details: Mapping[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": redact_sensitive_text(message),
        "request_id": request_id_for(request),
    }
    if details is not None:
        error["details"] = redact_error_details(details)
    return {"error": error}


def response_headers(request: Request, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    headers["X-Request-ID"] = request_id_for(request)
    return headers


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(request, "http_error", redact_sensitive_text(str(exc.detail))),
        headers=response_headers(request, exc.headers),
    )


async def application_exception_handler(request: Request, exc: ApplicationError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(request, exc.code, exc.message, exc.details),
        headers=response_headers(request),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=error_payload(
            request,
            "validation_error",
            "请求参数校验失败",
            redact_error_details(exc.errors()),
        ),
        headers=response_headers(request),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    structlog.get_logger("api").error(
        "unhandled_exception",
        request_id=request_id_for(request),
        path=redact_sensitive_text(request.url.path),
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content=error_payload(request, "internal_error", "服务内部错误"),
        headers=response_headers(request),
    )

# app/core/exceptions.py
"""Last-resort handler: any unhandled exception becomes a generic 500 JSON body (no internals leaked).

Expected failures raised by services are domain errors (app/core/errors.py), not handled here.
"""
from fastapi import Request
from fastapi.responses import JSONResponse


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Standardized 500 response for exceptions nothing else handled (Starlette re-raises it afterwards,
    so the server still logs the traceback)."""
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
    )

# app/core/exceptions.py
from fastapi import Request
from fastapi.responses import JSONResponse

# Kartik has changed this: Created standardized custom exception classes
class BusinessLogicError(Exception):
    def __init__(self, message: str, code: str = "BUSINESS_LOGIC_ERROR", status_code: int = 400):
        self.message = message
        self.code = code
        self.status_code = status_code

class ResourceNotFoundError(Exception):
    def __init__(self, message: str, code: str = "NOT_FOUND", status_code: int = 404):
        self.message = message
        self.code = code
        self.status_code = status_code

class ExternalServiceError(Exception):
        def __init__(self, message: str, code: str = "EXTERNAL_SERVICE_ERROR", status_code: int = 502):
            self.message = message
            self.code = code
            self.status_code = status_code

async def global_exception_handler(request: Request, exc: Exception):
    """
    Standardized Error Response Format for Global Exception Handling
    """
    if isinstance(exc, (BusinessLogicError, ResourceNotFoundError, ExternalServiceError)):
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "error": exc.message, "code": exc.code}
        )

    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "An unexpected system error occurred", "code": "INTERNAL_SERVER_ERROR"}
    )

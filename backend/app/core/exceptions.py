class DomainError(Exception):
    """业务领域异常的基类，由全局 handler 统一映射为 HTTP 响应。"""

    status_code: int = 400
    code: str = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class FetchError(DomainError):
    status_code = 422
    code = "fetch_failed"


class ValidationError(DomainError):
    status_code = 422
    code = "validation_error"

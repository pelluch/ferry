# Derived from decky-romm-sync (GPL-3.0-only):
#   py_modules/lib/errors.py
# Lifted in ferry's checkpoint 3 (2026-04). Modifications:
#   - Dropped classify_error / error_response (Decky RPC dict envelope, n/a here).
#   - Auth message references API key, not username/password.


class RommApiError(Exception):
    """Base exception for all RomM HTTP API errors."""

    status_code: int | None = None

    def __init__(self, message: str, url: str | None = None, method: str | None = None) -> None:
        self.url = url
        self.method = method
        super().__init__(message)


class RommAuthError(RommApiError):
    """401 Unauthorized — bad or expired API key."""

    status_code = 401


class RommForbiddenError(RommApiError):
    """403 Forbidden — token lacks required scopes."""

    status_code = 403


class RommNotFoundError(RommApiError):
    """404 Not Found — resource does not exist."""

    status_code = 404


class RommConflictError(RommApiError):
    """409 Conflict."""

    status_code = 409


class RommServerError(RommApiError):
    """5xx server errors (500, 502, 503, etc.)."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        url: str | None = None,
        method: str | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(message, url=url, method=method)


class RommConnectionError(RommApiError):
    """Network-level failures: connection refused, DNS failure, reset, etc."""


class RommTimeoutError(RommApiError):
    """Request timed out."""


class RommSSLError(RommApiError):
    """SSL certificate verification failure."""


class RommUnsupportedError(RommApiError):
    """Feature not available in the connected RomM server version."""

    def __init__(
        self,
        feature: str,
        min_version: str,
        url: str | None = None,
        method: str | None = None,
    ) -> None:
        self.feature = feature
        self.min_version = min_version
        super().__init__(
            f"{feature} requires RomM {min_version} or newer",
            url=url,
            method=method,
        )

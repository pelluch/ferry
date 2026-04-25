# Derived from decky-romm-sync (GPL-3.0-only):
#   py_modules/adapters/romm/http.py
# Lifted in ferry's checkpoint 3 (2026-04). Significant modifications:
#   - urllib → httpx (drops the lib/certifi_bundle.py dep; system CA store).
#   - Settings dict (mutable, by reference) → frozen RommConfig dataclass.
#   - Auth: Basic (base64 user:pass) → Bearer rmm_* API tokens.
#   - GET-only surface this checkpoint; download/POST/PUT/multipart land later.
#   - Decky-specific load_platform_map / resolve_system dropped (replaced by
#     ferry's frontend-profile abstraction in a later checkpoint).

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import httpx

from ferry.adapters.romm.errors import (
    RommApiError,
    RommAuthError,
    RommConflictError,
    RommConnectionError,
    RommForbiddenError,
    RommNotFoundError,
    RommServerError,
    RommSSLError,
    RommTimeoutError,
)
from ferry.config import RommConfig

DEFAULT_CONNECT_TIMEOUT = 30.0
DEFAULT_READ_TIMEOUT = 60.0
# Tuned for streaming large ROMs without holding much in memory.
DEFAULT_DOWNLOAD_TIMEOUT = httpx.Timeout(
    connect=DEFAULT_CONNECT_TIMEOUT,
    read=DEFAULT_READ_TIMEOUT,
    write=DEFAULT_READ_TIMEOUT,
    pool=DEFAULT_CONNECT_TIMEOUT,
)
_DOWNLOAD_BLOCK_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Outcome of a successful download — what's now on disk."""

    path: Path
    md5: str
    size: int


class RommHttpAdapter:
    """Low-level HTTP client for the RomM API.

    Owns an `httpx.Client` configured with the user's RomM URL, bearer token,
    and SSL preference. Handles error translation and exponential backoff
    retry on transient failures. Higher-level callers (`RommApi`) use the
    `get_json` method; download / POST / PUT / multipart land in later
    checkpoints as the features that need them ship.
    """

    _HTTP_STATUS_MAP: ClassVar[dict[int, type[RommApiError]]] = {
        400: RommApiError,
        401: RommAuthError,
        403: RommForbiddenError,
        404: RommNotFoundError,
        409: RommConflictError,
        429: RommServerError,
    }

    def __init__(
        self,
        config: RommConfig,
        logger: logging.Logger | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._client = client if client is not None else self._build_client(config)
        self._owns_client = client is None

    @staticmethod
    def _build_client(config: RommConfig) -> httpx.Client:
        return httpx.Client(
            base_url=config.url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=httpx.Timeout(
                connect=DEFAULT_CONNECT_TIMEOUT,
                read=DEFAULT_READ_TIMEOUT,
                write=DEFAULT_READ_TIMEOUT,
                pool=DEFAULT_CONNECT_TIMEOUT,
            ),
            verify=not config.allow_insecure_ssl,
            follow_redirects=True,
        )

    def __enter__(self) -> "RommHttpAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON resource from the RomM API. Returns parsed body.

        `params` are URL-encoded by httpx; pass scalar or list values, not
        already-encoded query strings.
        """
        return self._with_retry(self._do_get_json, path, params)

    def download(self, path: str, dest_path: Path) -> DownloadResult:
        """Stream a file to *dest_path*, computing md5 as we go.

        Writes to a sibling `.part` tempfile and atomically renames on
        success. The md5 is computed over the bytes we received — that's our
        source hash regardless of whether RomM advertised one (DESIGN.md
        notes hashing is opt-in server-side).

        `path` is passed to httpx unchanged; pre-encoded percent sequences
        are NOT re-encoded, so callers should encode filenames once with
        `urllib.parse.quote(safe="")` before assembling the path.
        """
        return self._with_retry(self._do_download, path, dest_path)

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    def _do_get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self._absolute_url(path)
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise self._translate_transport_error(exc, url, "GET") from exc

        if response.is_success:
            return response.json()
        raise self._translate_status(response, url, "GET")

    def _do_download(self, path: str, dest_path: Path) -> DownloadResult:
        url = self._absolute_url(path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_name(dest_path.name + ".part")

        md5 = hashlib.md5()
        size = 0
        try:
            with self._client.stream("GET", path) as response:
                if not response.is_success:
                    # Drain so the body is available if needed (small at error path).
                    response.read()
                    raise self._translate_status(response, url, "GET")

                advertised_total = self._content_length(response)
                with tmp_path.open("wb") as f:
                    for chunk in response.iter_bytes(chunk_size=_DOWNLOAD_BLOCK_SIZE):
                        if not chunk:
                            continue
                        f.write(chunk)
                        md5.update(chunk)
                        size += len(chunk)

                if advertised_total is not None and size != advertised_total:
                    raise RommApiError(
                        f"download truncated: got {size} bytes, expected {advertised_total} "
                        f"(GET {url})",
                        url=url,
                        method="GET",
                    )
                if size == 0 and advertised_total in (None, 0):
                    raise RommApiError(
                        f"download produced 0 bytes (GET {url})",
                        url=url,
                        method="GET",
                    )
        except httpx.HTTPError as exc:
            tmp_path.unlink(missing_ok=True)
            raise self._translate_transport_error(exc, url, "GET") from exc
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        tmp_path.replace(dest_path)
        return DownloadResult(path=dest_path, md5=md5.hexdigest(), size=size)

    @staticmethod
    def _content_length(response: httpx.Response) -> int | None:
        raw = response.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _absolute_url(self, path: str) -> str:
        return f"{self._config.url}{path}"

    def _translate_status(self, response: httpx.Response, url: str, method: str) -> RommApiError:
        code = response.status_code
        msg = f"HTTP {code}: {response.reason_phrase} ({method} {url})"
        cls = self._HTTP_STATUS_MAP.get(code)
        if cls is RommServerError:
            return RommServerError(
                f"Rate limited — too many requests ({method} {url})",
                status_code=code,
                url=url,
                method=method,
            )
        if cls is not None:
            return cls(msg, url=url, method=method)
        if code >= 500:
            return RommServerError(msg, status_code=code, url=url, method=method)
        return RommApiError(msg, url=url, method=method)

    @staticmethod
    def _translate_transport_error(exc: httpx.HTTPError, url: str, method: str) -> RommApiError:
        if isinstance(exc, httpx.TimeoutException):
            return RommTimeoutError(str(exc) or "request timed out", url=url, method=method)
        if isinstance(exc, httpx.ConnectError):
            # httpx wraps ssl errors as ConnectError; sniff the message.
            text = str(exc).lower()
            if "ssl" in text or "certificate" in text:
                return RommSSLError(str(exc), url=url, method=method)
            return RommConnectionError(str(exc), url=url, method=method)
        if isinstance(exc, httpx.NetworkError):
            return RommConnectionError(str(exc), url=url, method=method)
        return RommApiError(f"Unexpected error: {exc}", url=url, method=method)

    @staticmethod
    def is_retryable(exc: BaseException) -> bool:
        return isinstance(exc, (RommServerError, RommConnectionError, RommTimeoutError))

    def _with_retry(self, fn, *args, max_attempts: int = 3, base_delay: float = 1.0, **kwargs):
        last: BaseException | None = None
        for attempt in range(max_attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last = exc
                if attempt < max_attempts - 1 and self.is_retryable(exc):
                    delay = base_delay * (3**attempt)
                    self._logger.info(
                        "retry %d/%d after %.1fs: %s",
                        attempt + 1,
                        max_attempts,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                else:
                    raise
        raise last  # type: ignore[misc]  # pragma: no cover

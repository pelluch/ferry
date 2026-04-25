import httpx
import pytest
import respx

from ferry.adapters.romm import (
    RommApi,
    RommApiError,
    RommAuthError,
    RommConnectionError,
    RommForbiddenError,
    RommHttpAdapter,
    RommNotFoundError,
    RommServerError,
    RommTimeoutError,
)
from ferry.config import RommConfig

BASE_URL = "https://romm.example.tld"
API_KEY = "rmm_testkey_abcdef"


def make_config(*, allow_insecure_ssl: bool = False) -> RommConfig:
    return RommConfig(url=BASE_URL, api_key=API_KEY, allow_insecure_ssl=allow_insecure_ssl)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip retry backoff sleeps so tests run instantly."""
    monkeypatch.setattr("ferry.adapters.romm.http.time.sleep", lambda *_: None)


# ---------------------------------------------------------------------------
# RommHttpAdapter — get_json
# ---------------------------------------------------------------------------


@respx.mock
def test_get_json_returns_parsed_body() -> None:
    route = respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(200, json={"id": 1, "username": "pablo"})
    )
    with RommHttpAdapter(make_config()) as http:
        result = http.get_json("/api/users/me")
    assert result == {"id": 1, "username": "pablo"}
    assert route.called


@respx.mock
def test_get_json_sends_bearer_authorization_header() -> None:
    route = respx.get(f"{BASE_URL}/api/users/me").mock(return_value=httpx.Response(200, json={}))
    with RommHttpAdapter(make_config()) as http:
        http.get_json("/api/users/me")
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {API_KEY}"


# ---------------------------------------------------------------------------
# RommHttpAdapter — status code translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,exc_type",
    [
        (401, RommAuthError),
        (403, RommForbiddenError),
        (404, RommNotFoundError),
        (500, RommServerError),
        (502, RommServerError),
        (503, RommServerError),
    ],
)
@respx.mock
def test_status_codes_translate_to_typed_errors(status: int, exc_type: type[RommApiError]) -> None:
    respx.get(f"{BASE_URL}/api/x").mock(return_value=httpx.Response(status))
    with RommHttpAdapter(make_config()) as http, pytest.raises(exc_type) as ei:
        http.get_json("/api/x")
    assert ei.value.url == f"{BASE_URL}/api/x"
    assert ei.value.method == "GET"


@respx.mock
def test_unmapped_4xx_falls_back_to_base_error() -> None:
    respx.get(f"{BASE_URL}/api/x").mock(return_value=httpx.Response(418))
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommApiError) as ei:
        http.get_json("/api/x")
    # Must be the base class — not any of the typed subclasses.
    assert type(ei.value) is RommApiError


# ---------------------------------------------------------------------------
# RommHttpAdapter — retry
# ---------------------------------------------------------------------------


@respx.mock
def test_retries_on_5xx_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/api/x").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    with RommHttpAdapter(make_config()) as http:
        result = http.get_json("/api/x")
    assert result == {"ok": True}
    assert route.call_count == 3


@respx.mock
def test_does_not_retry_auth_errors() -> None:
    route = respx.get(f"{BASE_URL}/api/x").mock(return_value=httpx.Response(401))
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommAuthError):
        http.get_json("/api/x")
    assert route.call_count == 1


@respx.mock
def test_exhausts_retries_and_raises_last_error() -> None:
    route = respx.get(f"{BASE_URL}/api/x").mock(return_value=httpx.Response(500))
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommServerError):
        http.get_json("/api/x")
    assert route.call_count == 3


# ---------------------------------------------------------------------------
# RommHttpAdapter — transport errors
# ---------------------------------------------------------------------------


@respx.mock
def test_connection_error_translates() -> None:
    respx.get(f"{BASE_URL}/api/x").mock(side_effect=httpx.ConnectError("boom"))
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommConnectionError):
        http.get_json("/api/x")


@respx.mock
def test_timeout_translates() -> None:
    respx.get(f"{BASE_URL}/api/x").mock(side_effect=httpx.ReadTimeout("slow"))
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommTimeoutError):
        http.get_json("/api/x")


# ---------------------------------------------------------------------------
# RommApi — high-level surface
# ---------------------------------------------------------------------------


@respx.mock
def test_get_me_hits_users_me() -> None:
    route = respx.get(f"{BASE_URL}/api/users/me").mock(
        return_value=httpx.Response(200, json={"id": 7, "username": "p"})
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        user = api.get_me()
    assert user == {"id": 7, "username": "p"}
    assert route.called


@respx.mock
def test_list_collections_hits_collections() -> None:
    route = respx.get(f"{BASE_URL}/api/collections").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "Steam Deck"}])
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        cols = api.list_collections()
    assert cols == [{"id": 1, "name": "Steam Deck"}]
    assert route.called

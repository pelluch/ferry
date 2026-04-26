import hashlib

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


# ---------------------------------------------------------------------------
# RommApi.list_roms_in_collection — pagination + params
# ---------------------------------------------------------------------------


@respx.mock
def test_list_roms_returns_items_from_single_page() -> None:
    route = respx.get(f"{BASE_URL}/api/roms").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"id": 1}, {"id": 2}, {"id": 3}],
                "total": 3,
                "limit": 10000,
                "offset": 0,
            },
        )
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        roms = api.list_roms_in_collection(7)
    assert [r["id"] for r in roms] == [1, 2, 3]
    assert route.call_count == 1
    sent = route.calls.last.request
    # Query params we care about end up on the URL.
    assert "collection_id=7" in str(sent.url)
    assert "with_char_index=false" in str(sent.url)
    assert "with_filter_values=false" in str(sent.url)


@respx.mock
def test_list_roms_paginates_when_total_exceeds_page() -> None:
    """If total > page_size, the adapter walks offsets until items collected."""
    page_size = 2
    # Patch the adapter's page size to 2 for this test only — keeps the test
    # fast and exercises the pagination loop without huge fixtures.
    import ferry.adapters.romm.api as api_module

    original = api_module.ROMS_PAGE_SIZE
    api_module.ROMS_PAGE_SIZE = page_size
    try:
        responses = [
            httpx.Response(
                200,
                json={"items": [{"id": 1}, {"id": 2}], "total": 5, "limit": 2, "offset": 0},
            ),
            httpx.Response(
                200,
                json={"items": [{"id": 3}, {"id": 4}], "total": 5, "limit": 2, "offset": 2},
            ),
            httpx.Response(
                200,
                json={"items": [{"id": 5}], "total": 5, "limit": 2, "offset": 4},
            ),
        ]
        respx.get(f"{BASE_URL}/api/roms").mock(side_effect=responses)
        with RommHttpAdapter(make_config()) as http:
            api = RommApi(http)
            roms = api.list_roms_in_collection(7)
    finally:
        api_module.ROMS_PAGE_SIZE = original
    assert [r["id"] for r in roms] == [1, 2, 3, 4, 5]


@respx.mock
def test_list_roms_passes_group_by_meta_id_when_primary_only() -> None:
    route = respx.get(f"{BASE_URL}/api/roms").mock(
        return_value=httpx.Response(
            200, json={"items": [], "total": 0, "limit": 10000, "offset": 0}
        )
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.list_roms_in_collection(7, primary_only=True)
    sent = route.calls.last.request
    assert "group_by_meta_id=true" in str(sent.url)


@respx.mock
def test_list_roms_handles_empty_collection() -> None:
    respx.get(f"{BASE_URL}/api/roms").mock(
        return_value=httpx.Response(
            200, json={"items": [], "total": 0, "limit": 10000, "offset": 0}
        )
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        roms = api.list_roms_in_collection(7)
    assert roms == []


# ---------------------------------------------------------------------------
# RommHttpAdapter.download — streaming, hashing, atomicity
# ---------------------------------------------------------------------------


_PAYLOAD = b"this is the rom payload" * 100  # ~2KB
_PAYLOAD_MD5 = hashlib.md5(_PAYLOAD).hexdigest()  # computed once at module load


@respx.mock
def test_download_streams_to_dest_and_returns_hash(tmp_path) -> None:
    respx.get(f"{BASE_URL}/api/roms/42/content/Game.zip").mock(
        return_value=httpx.Response(200, content=_PAYLOAD)
    )
    dest = tmp_path / "out" / "Game.zip"
    with RommHttpAdapter(make_config()) as http:
        result = http.download("/api/roms/42/content/Game.zip", dest)

    assert result.path == dest
    assert result.md5 == _PAYLOAD_MD5
    assert result.size == len(_PAYLOAD)
    assert dest.read_bytes() == _PAYLOAD
    # No `.part` left behind on success.
    assert not (dest.parent / (dest.name + ".part")).exists()


@respx.mock
def test_download_creates_parent_dirs(tmp_path) -> None:
    respx.get(f"{BASE_URL}/api/x").mock(return_value=httpx.Response(200, content=_PAYLOAD))
    dest = tmp_path / "deep" / "nested" / "Game.zip"
    with RommHttpAdapter(make_config()) as http:
        http.download("/api/x", dest)
    assert dest.exists()


@respx.mock
def test_download_atomic_rename_no_partial_dest_on_404(tmp_path) -> None:
    respx.get(f"{BASE_URL}/api/x").mock(return_value=httpx.Response(404))
    dest = tmp_path / "Game.zip"
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommNotFoundError):
        http.download("/api/x", dest)
    # No file at dest, no `.part` either.
    assert not dest.exists()
    assert not (dest.parent / (dest.name + ".part")).exists()


@respx.mock
def test_download_truncated_response_raises(tmp_path) -> None:
    """Server says Content-Length=N but delivers less → integrity error."""
    # Build a response with Content-Length lying about size.
    respx.get(f"{BASE_URL}/api/x").mock(
        return_value=httpx.Response(
            200,
            content=_PAYLOAD,  # actual bytes
            headers={"Content-Length": str(len(_PAYLOAD) * 2)},  # claims more
        )
    )
    dest = tmp_path / "Game.zip"
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommApiError, match="truncated"):
        http.download("/api/x", dest)
    assert not dest.exists()


@respx.mock
def test_download_zero_bytes_no_content_length_raises(tmp_path) -> None:
    """No Content-Length AND no data delivered → suspicious; refuse."""
    respx.get(f"{BASE_URL}/api/x").mock(return_value=httpx.Response(200, content=b""))
    dest = tmp_path / "Game.zip"
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommApiError, match="0 bytes"):
        http.download("/api/x", dest)
    assert not dest.exists()


@respx.mock
def test_download_retries_on_5xx_then_succeeds(tmp_path) -> None:
    respx.get(f"{BASE_URL}/api/x").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, content=_PAYLOAD),
        ]
    )
    dest = tmp_path / "Game.zip"
    with RommHttpAdapter(make_config()) as http:
        result = http.download("/api/x", dest)
    assert result.md5 == _PAYLOAD_MD5
    assert dest.read_bytes() == _PAYLOAD


@respx.mock
def test_download_does_not_retry_auth_errors(tmp_path) -> None:
    route = respx.get(f"{BASE_URL}/api/x").mock(return_value=httpx.Response(401))
    dest = tmp_path / "Game.zip"
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommAuthError):
        http.download("/api/x", dest)
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# RommApi.download_rom — URL encoding (the lifted bug)
# ---------------------------------------------------------------------------


@respx.mock
def test_download_rom_encodes_filename_once(tmp_path) -> None:
    """Spaces/&/() in filenames are encoded once; `%XX` is not double-escaped."""
    # Filename has spaces, ampersand, parentheses — full v1 exit-criteria set.
    filename = "Sonic & Knuckles (USA).zip"
    expected_path = "/api/roms/123/content/Sonic%20%26%20Knuckles%20%28USA%29.zip"
    route = respx.get(f"{BASE_URL}{expected_path}").mock(
        return_value=httpx.Response(200, content=_PAYLOAD)
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        result = api.download_rom(123, filename, tmp_path / "out.zip")
    assert route.called
    assert result.size == len(_PAYLOAD)
    # Confirm only ONE round of encoding happened — no `%2520` substring.
    sent_url = str(route.calls.last.request.url)
    assert "%2520" not in sent_url


@respx.mock
def test_download_rom_handles_unicode_filename(tmp_path) -> None:
    """Non-ASCII filenames are URL-encoded as UTF-8 byte sequences."""
    filename = "ポケモン.zip"
    # UTF-8 bytes of "ポケモン" → percent-encoded.
    expected_path = "/api/roms/1/content/%E3%83%9D%E3%82%B1%E3%83%A2%E3%83%B3.zip"
    route = respx.get(f"{BASE_URL}{expected_path}").mock(
        return_value=httpx.Response(200, content=_PAYLOAD)
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.download_rom(1, filename, tmp_path / "out.zip")
    assert route.called
    assert (tmp_path / "out.zip").read_bytes() == _PAYLOAD


@respx.mock
def test_download_rom_handles_apostrophe(tmp_path) -> None:
    """Apostrophes in filenames (e.g., Castlevania II: Belmont's Revenge)."""
    filename = "Belmont's Revenge.zip"
    expected_path = "/api/roms/77/content/Belmont%27s%20Revenge.zip"
    route = respx.get(f"{BASE_URL}{expected_path}").mock(
        return_value=httpx.Response(200, content=_PAYLOAD)
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.download_rom(77, filename, tmp_path / "out.zip")
    assert route.called

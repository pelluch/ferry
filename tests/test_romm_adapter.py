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


@respx.mock
def test_4xx_with_json_detail_populates_payload_detail() -> None:
    """RomM's standard 4xx body shape is `{"detail": "..."}`. The
    parsed string is attached to the exception so callers can
    distinguish a real RomM error from a transport-layer 404
    (proxy can't reach RomM, wrong base URL, etc.) without parsing
    the message string."""
    respx.get(f"{BASE_URL}/api/x").mock(
        return_value=httpx.Response(404, json={"detail": "Save with ID 99 not found"})
    )
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommApiError) as ei:
        http.get_json("/api/x")
    assert ei.value.payload_detail == "Save with ID 99 not found"


@respx.mock
def test_4xx_without_json_body_leaves_payload_detail_none() -> None:
    """A 4xx whose body isn't JSON (e.g., proxy 404 with HTML) leaves
    `payload_detail` as None — gating recovery logic on this attribute
    avoids silently treating non-RomM errors as RomM errors."""
    respx.get(f"{BASE_URL}/api/x").mock(
        return_value=httpx.Response(404, content=b"<html>nginx 404</html>")
    )
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommApiError) as ei:
        http.get_json("/api/x")
    assert ei.value.payload_detail is None


@respx.mock
def test_4xx_with_json_body_but_no_detail_field_leaves_payload_detail_none() -> None:
    """A JSON 4xx body without a `detail` field doesn't fit RomM's shape;
    treat it the same as no JSON at all so callers don't conflate
    third-party errors with RomM errors."""
    respx.get(f"{BASE_URL}/api/x").mock(
        return_value=httpx.Response(404, json={"error": "something else"})
    )
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommApiError) as ei:
        http.get_json("/api/x")
    assert ei.value.payload_detail is None


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
# RommApi.list_roms / list_platforms — pagination, filters
# ---------------------------------------------------------------------------


@respx.mock
def test_list_platforms_hits_platforms() -> None:
    route = respx.get(f"{BASE_URL}/api/platforms").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "slug": "gba", "name": "GBA"}])
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        platforms = api.list_platforms()
    assert platforms == [{"id": 4, "slug": "gba", "name": "GBA"}]
    assert route.called


@respx.mock
def test_list_roms_with_platform_ids_passes_repeated_query_params() -> None:
    route = respx.get(f"{BASE_URL}/api/roms").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": 1}], "total": 1, "limit": 10000, "offset": 0}
        )
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.list_roms(platform_ids=[4, 12])
    sent = str(route.calls.last.request.url)
    assert "platform_ids=4" in sent
    assert "platform_ids=12" in sent
    assert "collection_id=" not in sent


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
        roms = api.list_roms(collection_id=7)
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
            roms = api.list_roms(collection_id=7)
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
        api.list_roms(collection_id=7, primary_only=True)
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
        roms = api.list_roms(collection_id=7)
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


# ---------------------------------------------------------------------------
# RommHttpAdapter — post_json + upload_multipart
# ---------------------------------------------------------------------------


@respx.mock
def test_post_json_sends_body_and_returns_response() -> None:
    route = respx.post(f"{BASE_URL}/api/x").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with RommHttpAdapter(make_config()) as http:
        result = http.post_json("/api/x", {"k": "v"})
    assert result == {"ok": True}
    sent = route.calls.last.request
    assert sent.headers["content-type"] == "application/json"
    assert b'"k":"v"' in sent.content or b'"k": "v"' in sent.content


@respx.mock
def test_post_json_status_translation_includes_detail() -> None:
    """4xx with `{"detail": "..."}` body surfaces the detail in the error message."""
    respx.post(f"{BASE_URL}/api/x").mock(
        return_value=httpx.Response(409, json={"detail": "Slot has a newer save"})
    )
    from ferry.adapters.romm import RommConflictError

    with RommHttpAdapter(make_config()) as http, pytest.raises(RommConflictError) as ei:
        http.post_json("/api/x", {})
    assert "Slot has a newer save" in str(ei.value)


@respx.mock
def test_upload_multipart_sends_file(tmp_path) -> None:
    file_path = tmp_path / "save.srm"
    file_path.write_bytes(b"battery save data")
    route = respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json={"id": 7, "file_name": "save.srm"})
    )
    with RommHttpAdapter(make_config()) as http:
        result = http.upload_multipart(
            "/api/saves", file_path, params={"rom_id": 42, "emulator": "RetroArch"}
        )
    assert result == {"id": 7, "file_name": "save.srm"}
    sent = route.calls.last.request
    assert "multipart/form-data" in sent.headers["content-type"]
    assert b"save.srm" in sent.content
    assert b"battery save data" in sent.content
    sent_url = str(sent.url)
    assert "rom_id=42" in sent_url
    assert "emulator=RetroArch" in sent_url


@respx.mock
def test_upload_multipart_put_for_in_place_update(tmp_path) -> None:
    file_path = tmp_path / "save.srm"
    file_path.write_bytes(b"newer data")
    route = respx.put(f"{BASE_URL}/api/saves/9").mock(
        return_value=httpx.Response(200, json={"id": 9})
    )
    with RommHttpAdapter(make_config()) as http:
        http.upload_multipart("/api/saves/9", file_path, method="PUT")
    assert route.called


# ---------------------------------------------------------------------------
# RommApi.list_saves
# ---------------------------------------------------------------------------


@respx.mock
def test_list_saves_includes_rom_id_in_query() -> None:
    route = respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "file_name": "save.srm"}])
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        saves = api.list_saves(42)
    assert saves == [{"id": 1, "file_name": "save.srm"}]
    assert "rom_id=42" in str(route.calls.last.request.url)


@respx.mock
def test_list_saves_with_device_and_slot() -> None:
    route = respx.get(f"{BASE_URL}/api/saves").mock(return_value=httpx.Response(200, json=[]))
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.list_saves(42, device_id="dev-123", slot="auto")
    sent = str(route.calls.last.request.url)
    assert "rom_id=42" in sent
    assert "device_id=dev-123" in sent
    assert "slot=auto" in sent


@respx.mock
def test_list_saves_returns_empty_list_on_unexpected_shape() -> None:
    """Defensive: if RomM returns a dict instead of list (shouldn't happen),
    don't blow up — surface as empty list and let callers handle it."""
    respx.get(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        assert api.list_saves(42) == []


# ---------------------------------------------------------------------------
# RommApi.upload_save
# ---------------------------------------------------------------------------


@respx.mock
def test_upload_save_post_path_for_new_save(tmp_path) -> None:
    file_path = tmp_path / "save.srm"
    file_path.write_bytes(b"data")
    route = respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json={"id": 5})
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        result = api.upload_save(42, file_path, emulator="RetroArch")
    assert result == {"id": 5}
    sent_url = str(route.calls.last.request.url)
    assert "rom_id=42" in sent_url
    assert "emulator=RetroArch" in sent_url
    assert "overwrite=" not in sent_url  # default omits the param


@respx.mock
def test_upload_save_put_path_when_save_id_provided(tmp_path) -> None:
    file_path = tmp_path / "save.srm"
    file_path.write_bytes(b"data")
    route = respx.put(f"{BASE_URL}/api/saves/9").mock(
        return_value=httpx.Response(200, json={"id": 9})
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.upload_save(42, file_path, emulator="RetroArch", save_id=9)
    assert route.called


@respx.mock
def test_upload_save_passes_overwrite_when_true(tmp_path) -> None:
    file_path = tmp_path / "save.srm"
    file_path.write_bytes(b"data")
    route = respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json={"id": 5})
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.upload_save(42, file_path, emulator="RetroArch", overwrite=True)
    assert "overwrite=true" in str(route.calls.last.request.url)


@respx.mock
def test_upload_save_propagates_409_as_conflict(tmp_path) -> None:
    """Stale upload — RomM returns 409 with detail; surfaces as RommConflictError."""
    from ferry.adapters.romm import RommConflictError

    file_path = tmp_path / "save.srm"
    file_path.write_bytes(b"data")
    respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(409, json={"detail": "Slot has a newer save"})
    )
    with RommHttpAdapter(make_config()) as http, pytest.raises(RommConflictError) as ei:
        api = RommApi(http)
        api.upload_save(42, file_path, emulator="RetroArch", device_id="dev-1", slot="auto")
    assert "Slot has a newer save" in str(ei.value)


@respx.mock
def test_upload_save_includes_device_and_slot(tmp_path) -> None:
    file_path = tmp_path / "save.srm"
    file_path.write_bytes(b"data")
    route = respx.post(f"{BASE_URL}/api/saves").mock(
        return_value=httpx.Response(200, json={"id": 5})
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.upload_save(
            42,
            file_path,
            emulator="RetroArch",
            device_id="dev-7",
            slot="quicksave",
        )
    sent = str(route.calls.last.request.url)
    assert "device_id=dev-7" in sent
    assert "slot=quicksave" in sent


# ---------------------------------------------------------------------------
# RommApi.download_save
# ---------------------------------------------------------------------------


@respx.mock
def test_download_save_streams_to_disk(tmp_path) -> None:
    payload = b"battery save bytes"
    route = respx.get(f"{BASE_URL}/api/saves/9/content").mock(
        return_value=httpx.Response(200, content=payload)
    )
    dest = tmp_path / "save.srm"
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        result = api.download_save(9, dest)
    assert dest.read_bytes() == payload
    assert result.size == len(payload)
    assert route.called


@respx.mock
def test_download_save_with_device_id_optimistic_default_false(tmp_path) -> None:
    """v3.5: `optimistic` defaults to False — server commits this device's
    `last_synced_at` only after `confirm_download`, so transient I/O failures
    naturally re-trigger next sync."""
    payload = b"data"
    route = respx.get(url__regex=rf"{BASE_URL}/api/saves/9/content.*").mock(
        return_value=httpx.Response(200, content=payload)
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.download_save(9, tmp_path / "out.srm", device_id="dev-1")
    sent = str(route.calls.last.request.url)
    assert "device_id=dev-1" in sent
    assert "optimistic=false" in sent


@respx.mock
def test_download_save_with_device_id_optimistic_true_explicit(tmp_path) -> None:
    """Explicit `optimistic=True` opts back into RomM's GET-time commit; no
    `optimistic` query param is sent (RomM's own default)."""
    payload = b"data"
    route = respx.get(url__regex=rf"{BASE_URL}/api/saves/9/content.*").mock(
        return_value=httpx.Response(200, content=payload)
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.download_save(9, tmp_path / "out.srm", device_id="dev-1", optimistic=True)
    sent = str(route.calls.last.request.url)
    assert "device_id=dev-1" in sent
    assert "optimistic=" not in sent


# ---------------------------------------------------------------------------
# RommApi.confirm_download / delete_saves
# ---------------------------------------------------------------------------


@respx.mock
def test_confirm_download_posts_device_id() -> None:
    route = respx.post(f"{BASE_URL}/api/saves/9/downloaded").mock(
        return_value=httpx.Response(200, json={"id": 9})
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.confirm_download(9, device_id="dev-1")
    assert route.called
    body = route.calls.last.request.content
    assert b"dev-1" in body


@respx.mock
def test_delete_saves_posts_id_list() -> None:
    route = respx.post(f"{BASE_URL}/api/saves/delete").mock(
        return_value=httpx.Response(200, json=[1, 2, 3])
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        deleted = api.delete_saves([1, 2, 3])
    assert deleted == [1, 2, 3]
    body = route.calls.last.request.content
    assert b'"saves"' in body


@respx.mock
def test_delete_saves_returns_empty_list_on_unexpected_shape() -> None:
    respx.post(f"{BASE_URL}/api/saves/delete").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        assert api.delete_saves([1]) == []


# ---------------------------------------------------------------------------
# RommApi.register_device
# ---------------------------------------------------------------------------


@respx.mock
def test_register_device_posts_payload() -> None:
    route = respx.post(f"{BASE_URL}/api/devices").mock(
        return_value=httpx.Response(
            201,
            json={
                "device_id": "uuid-abc",
                "name": "deck",
                "created_at": "2026-04-25T12:00:00Z",
            },
        )
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        result = api.register_device(
            name="deck",
            platform="linux",
            client="ferry",
            client_version="0.0.1",
        )
    assert result["device_id"] == "uuid-abc"
    body = route.calls.last.request.content
    assert b'"name":"deck"' in body or b'"name": "deck"' in body
    assert b'"client":"ferry"' in body or b'"client": "ferry"' in body
    assert b'"allow_existing":true' in body or b'"allow_existing": true' in body


@respx.mock
def test_register_device_idempotent_returns_existing() -> None:
    """Re-registering the same fingerprint returns 200 with the existing record."""
    respx.post(f"{BASE_URL}/api/devices").mock(
        return_value=httpx.Response(
            200,
            json={
                "device_id": "uuid-abc",
                "name": "deck",
                "created_at": "2026-04-25T12:00:00Z",
            },
        )
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        result = api.register_device(
            name="deck",
            platform="linux",
            client="ferry",
            client_version="0.0.1",
            hostname="deck",
            mac_address="00:11:22:33:44:55",
        )
    assert result["device_id"] == "uuid-abc"


@respx.mock
def test_register_device_includes_optional_fingerprint_fields() -> None:
    route = respx.post(f"{BASE_URL}/api/devices").mock(
        return_value=httpx.Response(201, json={"device_id": "uuid-x"})
    )
    with RommHttpAdapter(make_config()) as http:
        api = RommApi(http)
        api.register_device(
            name="deck",
            platform="linux",
            client="ferry",
            client_version="0.0.1",
            hostname="my-deck",
            mac_address="aa:bb:cc:dd:ee:ff",
        )
    body = route.calls.last.request.content
    assert b"my-deck" in body
    assert b"aa:bb:cc:dd:ee:ff" in body

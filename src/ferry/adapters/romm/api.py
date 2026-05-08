# Derived from decky-romm-sync (GPL-3.0-only):
#   py_modules/adapters/romm/romm_api.py
# Lifted in ferry's checkpoint 3 (2026-04). Significant modifications:
#   - Reduced to the ferry-needed surface (saves, devices, ROMs, collections,
#     platforms, users); dropped firmware / notes / virtual-collection /
#     heartbeat / metadata-summary endpoints not currently used.
#   - Merged decky's `download_save` and `download_save_content` into a
#     single `download_save(..., optimistic=...)`.
#   - `delete_server_saves` → `delete_saves` (the adapter is already
#     RomM-namespaced).
#   - URL params via httpx `params=...` rather than hand-built query strings,
#     so reserved characters round-trip correctly.
#   - `list_roms` paginates internally (page-and-merge loop) instead of
#     exposing limit/offset to callers.

import urllib.parse
from pathlib import Path
from typing import Any

from ferry.adapters.romm.http import DownloadResult, RommHttpAdapter

# RomM caps `limit` at 10000; we use the cap so a typical library fits in a
# single request and pagination only kicks in for very large collections.
ROMS_PAGE_SIZE = 10000


class RommApi:
    """Thin high-level wrapper over `RommHttpAdapter`.

    Surface grows checkpoint-by-checkpoint. v1 needs `get_me`,
    `list_collections`, and `list_roms_in_collection`; download / saves /
    achievements arrive later.
    """

    def __init__(self, http: RommHttpAdapter) -> None:
        self._http = http

    def get_me(self) -> dict[str, Any]:
        """GET /api/users/me — current user, including scopes and RA fields."""
        return self._http.get_json("/api/users/me")

    def list_collections(self) -> list[dict[str, Any]]:
        """GET /api/collections — manual user-created collections."""
        return self._http.get_json("/api/collections")

    def list_platforms(self) -> list[dict[str, Any]]:
        """GET /api/platforms — every platform RomM knows about (id, slug, name, ...).

        Used to resolve user-config `[sync].platforms` slugs (`"gba"`) to the
        integer ids RomM's filter API accepts.
        """
        return self._http.get_json("/api/platforms")

    def list_roms(
        self,
        *,
        collection_id: int | None = None,
        platform_ids: list[int] | None = None,
        primary_only: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /api/roms with optional filters; auto-paginated.

        Pass at most one filter shape per call. Combining `collection_id`
        with `platform_ids` server-side intersects (ROM must match all
        filters) — rarely what ferry's union-of-sources model wants.
        Callers fetch each source separately and dedup client-side.

        Skips RomM's UI-only metadata (`with_char_index`,
        `with_filter_values`) to keep payloads small. When `primary_only` is
        True, RomM groups by metadata ID and returns the user's
        `is_main_sibling`-flagged ROM per group (DESIGN.md §5.1).
        """
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            params: dict[str, Any] = {
                "limit": ROMS_PAGE_SIZE,
                "offset": offset,
                "with_char_index": "false",
                "with_filter_values": "false",
                "order_by": "id",
                "order_dir": "asc",
            }
            if collection_id is not None:
                params["collection_id"] = collection_id
            if platform_ids:
                params["platform_ids"] = list(platform_ids)
            if primary_only:
                params["group_by_meta_id"] = "true"
            page = self._http.get_json("/api/roms", params=params)
            page_items = page.get("items") or []
            items.extend(page_items)
            total = page.get("total")
            if not isinstance(total, int) or len(items) >= total or not page_items:
                break
            offset += ROMS_PAGE_SIZE
        return items

    def download_rom(
        self,
        rom_id: int,
        url_filename: str,
        dest_path: Path,
    ) -> DownloadResult:
        """Stream a ROM's content to *dest_path*.

        `url_filename` is the value RomM uses for the response's
        Content-Disposition (and to disambiguate path components in logs);
        the actual ROM is identified by `rom_id`. Pass `rom['fs_name']` from
        a /api/roms response.

        The filename is URL-encoded with `safe=""` so all reserved characters
        — `&`, `(`, `)`, spaces, unicode — are escaped exactly once before
        the path goes to httpx (which is idempotent on `%XX` sequences,
        sidestepping the double-URL-encode bug from decky-romm-sync).
        """
        encoded = urllib.parse.quote(url_filename, safe="")
        path = f"/api/roms/{rom_id}/content/{encoded}"
        return self._http.download(path, dest_path)

    # ------------------------------------------------------------------
    # Saves (DESIGN.md §5.3)
    # ------------------------------------------------------------------

    def list_saves(
        self,
        rom_id: int | None = None,
        *,
        device_id: str | None = None,
        slot: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/saves — every save for the user, optionally filtered.

        With no `rom_id`, returns ALL the user's saves in a single call —
        the bulk shape ferry's save sync flow uses to avoid an N+1 fan-out.
        Pass `rom_id` to filter server-side when inspecting a single ROM.
        """
        params: dict[str, Any] = {}
        if rom_id is not None:
            params["rom_id"] = rom_id
        if device_id is not None:
            params["device_id"] = device_id
        if slot is not None:
            params["slot"] = slot
        result = self._http.get_json("/api/saves", params=params)
        return result if isinstance(result, list) else []

    def upload_save(
        self,
        rom_id: int,
        file_path: Path,
        emulator: str,
        *,
        save_id: int | None = None,
        device_id: str | None = None,
        slot: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """POST /api/saves (new) or PUT /api/saves/{id} (update existing).

        `device_id` participates in RomM's conflict detection: if the slot
        has been updated since this device's last sync, the server returns
        409. Caller resolves via `domain.save_conflicts.resolve_newest` and
        retries with `overwrite=True`.
        """
        params: dict[str, Any] = {"rom_id": rom_id, "emulator": emulator}
        if device_id is not None:
            params["device_id"] = device_id
        if slot is not None:
            params["slot"] = slot
        if overwrite:
            params["overwrite"] = "true"

        if save_id is not None:
            return self._http.upload_multipart(
                f"/api/saves/{save_id}", file_path, method="PUT", params=params
            )
        return self._http.upload_multipart("/api/saves", file_path, method="POST", params=params)

    def download_save(
        self,
        save_id: int,
        dest_path: Path,
        *,
        device_id: str | None = None,
        optimistic: bool = True,
    ) -> DownloadResult:
        """GET /api/saves/{id}/content — stream a save to disk.

        With `device_id` and `optimistic=True` (RomM's default), the server
        records the device's `last_synced_at` as part of the download —
        saves an extra round-trip vs. confirming after the fact. Pass
        `optimistic=False` and call `confirm_download` separately when the
        client wants to commit only after a successful local write.
        """
        path = f"/api/saves/{save_id}/content"
        params: dict[str, Any] = {}
        if device_id is not None:
            params["device_id"] = device_id
            if not optimistic:
                params["optimistic"] = "false"
        if params:
            qs = urllib.parse.urlencode(params)
            path = f"{path}?{qs}"
        return self._http.download(path, dest_path)

    def confirm_download(self, save_id: int, device_id: str) -> dict[str, Any]:
        """POST /api/saves/{id}/downloaded — used when `optimistic=False`."""
        return self._http.post_json(
            f"/api/saves/{save_id}/downloaded",
            {"device_id": device_id},
        )

    def delete_saves(self, save_ids: list[int]) -> list[int]:
        """POST /api/saves/delete — bulk delete by ID. Returns ids actually deleted."""
        result = self._http.post_json("/api/saves/delete", {"saves": save_ids})
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Devices (RomM's per-device sync model)
    # ------------------------------------------------------------------

    def register_device(
        self,
        *,
        name: str,
        platform: str,
        client: str,
        client_version: str,
        hostname: str | None = None,
        mac_address: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/devices — register this client as a sync device.

        RomM dedups by fingerprint (mac_address/hostname/platform) when
        `allow_existing=true` (the default we send). On idempotent
        re-registration the server returns 200 with the existing
        `device_id`; on first-time registration it returns 201 with the
        newly-minted UUID. Either way the response carries
        `{device_id, name, created_at}`.
        """
        body: dict[str, Any] = {
            "name": name,
            "platform": platform,
            "client": client,
            "client_version": client_version,
            "allow_existing": True,
        }
        if hostname is not None:
            body["hostname"] = hostname
        if mac_address is not None:
            body["mac_address"] = mac_address
        return self._http.post_json("/api/devices", body)

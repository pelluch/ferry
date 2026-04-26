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

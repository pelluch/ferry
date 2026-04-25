from typing import Any

from ferry.adapters.romm.http import RommHttpAdapter

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
        """GET /api/collections — collections visible to the current user."""
        return self._http.get_json("/api/collections")

    def list_roms_in_collection(
        self,
        collection_id: int,
        *,
        primary_only: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /api/roms?collection_id=… — auto-paginated; returns all rows.

        Skips RomM's UI-only metadata (`with_char_index`, `with_filter_values`)
        to keep the response payload small. When `primary_only` is True, RomM
        groups by metadata ID and returns the user's `is_main_sibling`-flagged
        ROM per group (DESIGN.md §5.1).
        """
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            params: dict[str, Any] = {
                "collection_id": collection_id,
                "limit": ROMS_PAGE_SIZE,
                "offset": offset,
                "with_char_index": "false",
                "with_filter_values": "false",
                "order_by": "id",
                "order_dir": "asc",
            }
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

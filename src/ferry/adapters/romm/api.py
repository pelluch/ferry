from typing import Any

from ferry.adapters.romm.http import RommHttpAdapter


class RommApi:
    """Thin high-level wrapper over `RommHttpAdapter`.

    Surface grows checkpoint-by-checkpoint. v1's `ferry ping` only needs
    `get_me` and `list_collections`; sync orchestration adds the rest.
    """

    def __init__(self, http: RommHttpAdapter) -> None:
        self._http = http

    def get_me(self) -> dict[str, Any]:
        """GET /api/users/me — current user, including scopes and RA fields."""
        return self._http.get_json("/api/users/me")

    def list_collections(self) -> list[dict[str, Any]]:
        """GET /api/collections — collections visible to the current user."""
        return self._http.get_json("/api/collections")

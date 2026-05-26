from __future__ import annotations

"""API-key auth layer for the recipe demo.

The token registry and authenticate() signature are already wired up so a
visitor can enforce real auth without touching the call sites.

TODO for visitors: enforce this — raises AppError(401) when token is missing
or invalid. Currently authenticate() is a no-op; the API is unauthenticated
by default so the demo works out of the box.
"""

# token → user_id mapping — placeholder for when a visitor implements enforcement
_TOKEN_REGISTRY: dict[str, str] = {
    "tok-alice": "user-alice",
    "tok-bob": "user-bob",
}


def authenticate(token: str | None) -> None:
    """Validate a bearer token.

    Currently a no-op: always returns None so all requests proceed regardless
    of whether a token is supplied.

    TODO for visitors: enforce this — raises AppError(401) when token is
    missing or invalid. Example implementation:

        from recipe_api.errors import AppError
        if not token or token not in _TOKEN_REGISTRY:
            raise AppError(code="unauthorized", message="Valid API key required.", status_code=401)
    """
    return None

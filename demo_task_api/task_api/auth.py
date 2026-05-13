from __future__ import annotations

"""Simple API-key auth layer for the demo.

Real auth would use JWT or OAuth. This is a demo-scoped stand-in that
validates a bearer token against a static registry and returns the
associated User.
"""

from task_api.errors import AppError
from task_api.models import User
from task_api.store import get_user

# token → user_id mapping
_TOKEN_REGISTRY: dict[str, str] = {
    "tok-alice": "user-1",
    "tok-bob": "user-2",
}


def authenticate(token: str | None) -> User:
    """Validate a bearer token and return the associated User.

    Raises AppError(401) if the token is missing or invalid.
    """
    if not token:
        raise AppError(code="missing_token", message="Authentication required.", status_code=401)
    user_id = _TOKEN_REGISTRY.get(token)
    if not user_id:
        raise AppError(code="invalid_token", message="Invalid or expired token.", status_code=401)
    user = get_user(user_id)
    if user is None:
        raise AppError(code="user_not_found", message="Associated user not found.", status_code=401)
    return user


def require_admin(user: User) -> None:
    """Raise AppError(403) if the user is not an admin."""
    if user.role != "admin":
        raise AppError(code="forbidden", message="Admin access required.", status_code=403)

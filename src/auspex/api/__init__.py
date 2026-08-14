"""FastAPI app, Entra JWT auth, and routes (arc42 §7 "app-auspex-api")."""

from __future__ import annotations

from auspex.api.app import create_app
from auspex.api.auth import AuthenticatedUser, get_current_user

__all__ = ["create_app", "AuthenticatedUser", "get_current_user"]

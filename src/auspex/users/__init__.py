"""Multi-user administration: registration, lifecycle and roles.

This package owns *who* an authenticated principal is to Auspex, as opposed
to :mod:`auspex.api.auth`, which only proves *that* they are who the token
says. Authentication is necessary but never sufficient: every ``/api`` route
is additionally gated on an ``app_users`` record maintained here.
"""

from __future__ import annotations

from auspex.users.deletion import (
    ACCEPTED_CONFIRMATION_PHRASES,
    CONFIRMATION_PHRASE,
    AccountDeletionService,
    DeletionConfirmationError,
)
from auspex.users.onboarding import OnboardingError, OnboardingService
from auspex.users.service import (
    AppUserService,
    LastAdminError,
    UserLifecycleError,
    UserNotFoundError,
)

__all__ = [
    "ACCEPTED_CONFIRMATION_PHRASES",
    "CONFIRMATION_PHRASE",
    "AccountDeletionService",
    "AppUserService",
    "DeletionConfirmationError",
    "LastAdminError",
    "OnboardingError",
    "OnboardingService",
    "UserLifecycleError",
    "UserNotFoundError",
]

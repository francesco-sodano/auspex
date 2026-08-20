"""Guided onboarding endpoints for an approved user (arc42 §5.7, §8.3).

Reachable only in the ``APPROVED_NEEDS_ONBOARDING`` state. Each step is a
full ``PUT``-style replace, so a retried request converges rather than
duplicating, and a user who abandons the flow resumes exactly where they
stopped. ``POST /complete`` is the single transition to ``ACTIVE`` and is
itself idempotent.

The identity is always the caller's own token; no endpoint here accepts a
``user_id``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from auspex.api.access import CurrentUser, require_onboarding_user
from auspex.api.deps import (
    get_app_user_service,
    get_onboarding_repo,
    get_portfolio_ledger_service,
    get_user_settings_repo,
)
from auspex.models.app_user import UserRole, UserStatus
from auspex.models.onboarding import (
    InitialPortfolio,
    OnboardingAcknowledgements,
    OnboardingPreferences,
    OnboardingState,
    OnboardingStep,
)
from auspex.portfolio.ledger_service import (
    PortfolioLedgerService,
    PortfolioLedgerValidationError,
)
from auspex.users.onboarding import OnboardingError, OnboardingService
from auspex.users.service import AppUserService, UserLifecycleError

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class OnboardingStateOut(BaseModel):
    """Onboarding progress *and* the resulting session state.

    Carries both shapes so a client can drive the wizard from one response
    without a second round trip to ``/api/session`` after every step.
    """

    user_id: str
    current_step: OnboardingStep
    next_step: OnboardingStep
    completed_steps: list[OnboardingStep]
    complete: bool
    preferences: OnboardingPreferences | None = None
    acknowledgements: OnboardingAcknowledgements | None = None
    initial_portfolio: InitialPortfolio | None = None
    # --- session projection ---
    status: UserStatus | None = None
    role: UserRole | None = None
    email: str | None = None
    display_name: str | None = None
    onboarding_completed: bool = False
    created_at: str | None = None
    approved_at: str | None = None
    deletion_status: str | None = None

    @classmethod
    def from_state(cls, state: OnboardingState, current: CurrentUser | None = None) -> OnboardingStateOut:
        app_user = current.app_user if current is not None else None
        return cls(
            user_id=state.user_id,
            current_step=state.current_step,
            next_step=state.next_step(),
            completed_steps=state.completed_steps(),
            complete=state.is_complete,
            preferences=state.preferences,
            acknowledgements=state.acknowledgements,
            initial_portfolio=state.initial_portfolio,
            status=app_user.status if app_user else None,
            role=app_user.role if app_user else None,
            email=app_user.email if app_user else None,
            display_name=app_user.display_name if app_user else None,
            onboarding_completed=bool(app_user and app_user.onboarding_completed_at is not None),
            created_at=app_user.registered_at.isoformat() if app_user else None,
            approved_at=app_user.approved_at.isoformat() if app_user and app_user.approved_at else None,
            deletion_status=(
                app_user.status.value
                if app_user and app_user.status in (UserStatus.DELETION_PENDING, UserStatus.DELETED)
                else None
            ),
        )


def _service(onboarding_repo, settings_repo, ledger=None) -> OnboardingService:
    return OnboardingService(onboarding_repo, settings_repo, ledger)


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("", response_model=OnboardingStateOut)
async def get_onboarding(
    current: CurrentUser = Depends(require_onboarding_user),
    onboarding_repo=Depends(get_onboarding_repo),
    settings_repo=Depends(get_user_settings_repo),
) -> OnboardingStateOut:
    state = await _service(onboarding_repo, settings_repo).get_state(current.user_id)
    return OnboardingStateOut.from_state(state, current)


@router.put("/preferences", response_model=OnboardingStateOut)
async def set_preferences(
    request: OnboardingPreferences,
    current: CurrentUser = Depends(require_onboarding_user),
    onboarding_repo=Depends(get_onboarding_repo),
    settings_repo=Depends(get_user_settings_repo),
) -> OnboardingStateOut:
    try:
        state = await _service(onboarding_repo, settings_repo).set_preferences(current.user_id, request)
    except OnboardingError as exc:
        raise _bad_request(exc) from exc
    return OnboardingStateOut.from_state(state, current)


@router.put("/acknowledgements", response_model=OnboardingStateOut)
async def set_acknowledgements(
    request: OnboardingAcknowledgements,
    current: CurrentUser = Depends(require_onboarding_user),
    onboarding_repo=Depends(get_onboarding_repo),
    settings_repo=Depends(get_user_settings_repo),
) -> OnboardingStateOut:
    try:
        state = await _service(onboarding_repo, settings_repo).set_acknowledgements(current.user_id, request)
    except OnboardingError as exc:
        raise _bad_request(exc) from exc
    return OnboardingStateOut.from_state(state, current)


@router.put("/initial-portfolio", response_model=OnboardingStateOut)
async def set_initial_portfolio(
    request: InitialPortfolio,
    current: CurrentUser = Depends(require_onboarding_user),
    onboarding_repo=Depends(get_onboarding_repo),
    settings_repo=Depends(get_user_settings_repo),
) -> OnboardingStateOut:
    """Declare the starting ledger.

    Rejected unless it is viable: opening CHF cash above zero, or at least
    one position with a positive quantity. A projection with neither cannot
    produce a meaningful recommendation, so admitting it would only strand
    the user in an empty product.
    """

    try:
        state = await _service(onboarding_repo, settings_repo).set_initial_portfolio(current.user_id, request)
    except OnboardingError as exc:
        raise _bad_request(exc) from exc
    return OnboardingStateOut.from_state(state, current)


@router.post("/portfolio", response_model=OnboardingStateOut)
async def set_initial_portfolio_compat(
    request: InitialPortfolio,
    current: CurrentUser = Depends(require_onboarding_user),
    onboarding_repo=Depends(get_onboarding_repo),
    settings_repo=Depends(get_user_settings_repo),
) -> OnboardingStateOut:
    """``POST /api/onboarding/portfolio`` — alias of :func:`set_initial_portfolio`."""

    return await set_initial_portfolio(request, current, onboarding_repo, settings_repo)


@router.post("/complete", response_model=OnboardingStateOut)
async def complete_onboarding(
    current: CurrentUser = Depends(require_onboarding_user),
    onboarding_repo=Depends(get_onboarding_repo),
    settings_repo=Depends(get_user_settings_repo),
    ledger: PortfolioLedgerService = Depends(get_portfolio_ledger_service),
    users: AppUserService = Depends(get_app_user_service),
) -> OnboardingStateOut:
    """Persist settings, seed the ledger, and activate the account."""

    service = _service(onboarding_repo, settings_repo, ledger)
    try:
        state = await service.complete(current.user_id)
    except OnboardingError as exc:
        raise _bad_request(exc) from exc
    except PortfolioLedgerValidationError as exc:
        raise _bad_request(exc) from exc
    try:
        activated = await users.complete_onboarding(current.user_id)
    except UserLifecycleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return OnboardingStateOut.from_state(
        state, CurrentUser(authenticated=current.authenticated, app_user=activated)
    )

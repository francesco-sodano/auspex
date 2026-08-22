"""Registration and session status — the only product routes an
unapproved-but-authenticated principal may call (arc42 §11).

Every other ``/api`` route is gated on an ``ACTIVE`` lifecycle state. These
three are gated on authentication only, because a user who cannot register
and cannot ask "what is my status?" has no way to ever become active.

None of these endpoints accept a ``user_id``: identity comes exclusively
from the validated Entra token, and no endpoint here can read anything about
any other user.
"""

from __future__ import annotations

from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from auspex.api.access import STATUS_DETAIL, CurrentUser, get_app_user
from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_app_user_service, get_onboarding_repo, get_user_settings_repo
from auspex.api.rate_limit import SlidingWindowRateLimiter, get_rate_limiter
from auspex.models.app_user import UserRole, UserStatus
from auspex.models.onboarding import (
    OnboardingAcknowledgements,
    OnboardingPreferences,
    OnboardingStep,
)
from auspex.models.user_settings import (
    InvestmentHorizon,
    InvestmentObjective,
    RiskProfile,
    migrate_investment_horizon,
)
from auspex.settings import Settings, get_settings
from auspex.users.onboarding import OnboardingError, OnboardingService
from auspex.users.service import AppUserService, UserLifecycleError

#: The five regulatory acknowledgements, in the order they are presented.
#: Sourced from the onboarding model so the two surfaces cannot drift apart.
ACKNOWLEDGEMENT_FIELDS: tuple[str, ...] = tuple(
    name
    for name in OnboardingAcknowledgements.model_fields
    if name.endswith("_acknowledged")
)

router = APIRouter(prefix="/session", tags=["session"])


class SessionOut(BaseModel):
    """Everything the SPA needs to decide which screen to show.

    Deliberately narrow: the caller's own lifecycle state and role, plus
    where onboarding stands. No other user's existence is implied and no
    private product data appears here.
    """

    user_id: str
    status: UserStatus
    role: UserRole
    registered: bool
    can_access_product: bool
    is_admin: bool
    email: str | None = None
    display_name: str | None = None
    detail: str | None = None
    onboarding_required: bool = False
    onboarding_completed: bool = False
    onboarding_next_step: OnboardingStep | None = None
    deletion_in_progress: bool = False
    deletion_status: str | None = None
    created_at: str | None = None
    approved_at: str | None = None


class RegisterRequest(BaseModel):
    """Registration takes no identity fields at all — see module docstring.

    Preferences and the five regulatory acknowledgements may optionally
    accompany the request. Both are stored as the corresponding *onboarding*
    steps rather than applied directly, because a pending user has no
    settings of their own until they are approved and complete onboarding.

    Capturing acknowledgements here matters: a client that presents the
    disclosures during sign-up would otherwise have them silently dropped,
    and the user would be approved only to find onboarding still demanding a
    step they already completed — with ``POST /api/onboarding/complete``
    failing until they redo it. The dedicated
    ``PUT /api/onboarding/acknowledgements`` endpoint remains the resumable
    path for clients that collect them after approval instead.

    Acknowledgements are all-or-nothing: supplying some but not all five, or
    supplying any as ``false``, is rejected rather than half-stored. A
    partial regulatory acknowledgement is not an acknowledgement.
    """

    model_config = ConfigDict(extra="ignore")

    accepted_terms: bool = True
    display_name: str | None = None
    risk_profile: RiskProfile | None = None
    investment_horizon: InvestmentHorizon | None = None
    investment_objective: InvestmentObjective | None = None
    cash_reserve_chf: str | None = None
    directional_only_acknowledged: bool | None = None
    no_guarantee_acknowledged: bool | None = None
    not_financial_advice_acknowledged: bool | None = None
    market_loss_acknowledged: bool | None = None
    independent_decision_acknowledged: bool | None = None
    acknowledgement_version: str | None = None

    @field_validator("investment_horizon", mode="before")
    @classmethod
    def _accept_legacy_horizon(cls, value: object) -> object:
        return migrate_investment_horizon(value)

    @field_validator("display_name")
    @classmethod
    def _clean_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        if not cleaned:
            return None
        if len(cleaned) > 120:
            raise ValueError("display_name supports at most 120 characters")
        return cleaned

    @model_validator(mode="after")
    def _acknowledgements_are_all_or_nothing(self) -> RegisterRequest:
        supplied = [value for value in self._acknowledgement_flags().values() if value is not None]
        if not supplied:
            return self
        if len(supplied) != len(ACKNOWLEDGEMENT_FIELDS) or not all(supplied):
            raise ValueError(
                "all five decision-support acknowledgements must be supplied and true"
            )
        return self

    def _acknowledgement_flags(self) -> dict[str, bool | None]:
        return {name: getattr(self, name) for name in ACKNOWLEDGEMENT_FIELDS}

    def preferences(self) -> OnboardingPreferences | None:
        supplied = {
            key: value
            for key, value in {
                "risk_profile": self.risk_profile,
                "investment_horizon": self.investment_horizon,
                "investment_objective": self.investment_objective,
                "cash_reserve_chf": self.cash_reserve_chf,
            }.items()
            if value is not None
        }
        return OnboardingPreferences(**supplied) if supplied else None

    def acknowledgements(self) -> OnboardingAcknowledgements | None:
        """The five acknowledgements, or ``None`` when the client omitted them.

        Validation has already guaranteed all-or-nothing, so reaching here
        with a partial set is impossible.
        """

        flags = self._acknowledgement_flags()
        if any(value is None for value in flags.values()):
            return None
        supplied = {name: True for name in flags}
        if self.acknowledgement_version:
            supplied["acknowledgement_version"] = self.acknowledgement_version
        return OnboardingAcknowledgements(**supplied)


def _session_out(
    current: CurrentUser,
    *,
    onboarding_next_step: OnboardingStep | None = None,
) -> SessionOut:
    app_user = current.app_user
    return SessionOut(
        user_id=current.user_id,
        status=current.status,
        role=current.role,
        registered=app_user is not None,
        can_access_product=app_user is not None and app_user.can_access_product(),
        is_admin=current.is_admin,
        email=app_user.email if app_user else current.authenticated.email,
        display_name=app_user.display_name if app_user else current.authenticated.display_name,
        detail=STATUS_DETAIL.get(current.status),
        onboarding_required=current.status is UserStatus.APPROVED_NEEDS_ONBOARDING,
        onboarding_completed=bool(app_user and app_user.onboarding_completed_at is not None),
        onboarding_next_step=onboarding_next_step,
        deletion_in_progress=current.status is UserStatus.DELETION_PENDING,
        deletion_status=(
            current.status.value
            if current.status in (UserStatus.DELETION_PENDING, UserStatus.DELETED)
            else None
        ),
        created_at=app_user.registered_at.isoformat() if app_user else None,
        approved_at=app_user.approved_at.isoformat() if app_user and app_user.approved_at else None,
    )


async def _with_onboarding_step(current: CurrentUser, onboarding_repo, settings_repo) -> SessionOut:
    if current.status is not UserStatus.APPROVED_NEEDS_ONBOARDING:
        return _session_out(current)
    service = OnboardingService(onboarding_repo, settings_repo)
    state = await service.get_state(current.user_id)
    return _session_out(current, onboarding_next_step=state.next_step())


@router.get("", response_model=SessionOut)
async def get_session(
    current: CurrentUser = Depends(get_app_user),
    onboarding_repo=Depends(get_onboarding_repo),
    settings_repo=Depends(get_user_settings_repo),
) -> SessionOut:
    """The caller's own lifecycle state. Never 403s — it *is* the status page."""

    return await _with_onboarding_step(current, onboarding_repo, settings_repo)


@router.get("/status", response_model=SessionOut)
async def get_session_status(
    current: CurrentUser = Depends(get_app_user),
    onboarding_repo=Depends(get_onboarding_repo),
    settings_repo=Depends(get_user_settings_repo),
) -> SessionOut:
    """Alias of :func:`get_session` for clients polling while pending."""

    return await _with_onboarding_step(current, onboarding_repo, settings_repo)


@router.post("/register", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    service: AppUserService = Depends(get_app_user_service),
    onboarding_repo=Depends(get_onboarding_repo),
    settings_repo=Depends(get_user_settings_repo),
    limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
    settings: Settings = Depends(get_settings),
) -> SessionOut:
    """Register the authenticated principal. Idempotent.

    A repeat call returns the current state rather than failing, so a client
    that retries a lost response cannot end up wedged. The very first
    administrator is bootstrapped here when the token's verified email
    matches ``Settings.initial_admin_email`` and no administrator exists yet;
    authority then binds to that principal's immutable object ID.
    """

    await limiter.check(
        scope="registration",
        key=user.user_id,
        limit=settings.registration_rate_limit,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if not request.accepted_terms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="registration requires accepting the terms",
        )
    try:
        app_user = await service.register(
            provider_user_id=user.provider_user_id or user.user_id,
            email=user.email,
            email_verified=user.email_verified,
            display_name=(request.display_name or user.display_name),
            identity_provider=user.identity_provider,
        )
    except UserLifecycleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    preferences = request.preferences()
    acknowledgements = request.acknowledgements()
    can_write_onboarding = app_user.status in {
        UserStatus.PENDING_APPROVAL,
        UserStatus.APPROVED_NEEDS_ONBOARDING,
    }
    if can_write_onboarding and (
        preferences is not None or acknowledgements is not None
    ):
        # Carried into onboarding so the user does not retype them; never
        # applied as live settings, which only exist once they are ACTIVE.
        # `OnboardingError` here means onboarding is already complete (a
        # returning ACTIVE user re-registering), which is not a failure.
        async with service.user_operation(
            app_user.user_id,
            require_active=False,
        ):
            onboarding = OnboardingService(onboarding_repo, settings_repo)
            if preferences is not None:
                with suppress(OnboardingError):
                    await onboarding.set_preferences(
                        app_user.user_id,
                        preferences,
                    )
            if acknowledgements is not None:
                with suppress(OnboardingError):
                    await onboarding.set_acknowledgements(
                        app_user.user_id,
                        acknowledgements,
                    )

    current = CurrentUser(authenticated=user, app_user=app_user)
    return await _with_onboarding_step(current, onboarding_repo, settings_repo)

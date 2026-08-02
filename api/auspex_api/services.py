from .app_users import AppUserRepository
from .auth import parse_swa_principal
from .models import AppUser, OnboardingProfile, RegistrationAcknowledgments


DOCUMENT_VERSIONS = {
    "adult_declaration": "2026-01",
    "risk_disclosure": "2026-01",
    "advisory_disclaimer": "2026-01",
    "terms": "2026-01",
    "privacy": "2026-01",
}


class RegistrationRequiredError(PermissionError):
    pass


class AuthorizationError(PermissionError):
    pass


class UserNotFoundError(LookupError):
    pass


class InvalidTransitionError(ValueError):
    pass


class IdentityService:
    def __init__(self, users: AppUserRepository, clock=None) -> None:
        self._users = users
        self._clock = clock

    def _principal(self, principal_header):
        return parse_swa_principal(principal_header)

    def _admin(self, principal_header):
        principal = self._principal(principal_header)
        if not principal.is_swa_admin:
            raise AuthorizationError("SWA admin role is required")
        user = self._users.get_by_principal(principal)
        if user is None:
            user, _ = self._users.create_admin(principal)
        if user.status != "active" or user.role != "admin":
            raise AuthorizationError("Active Auspex admin role is required")
        return principal, user

    def me(self, principal_header) -> AppUser:
        principal = self._principal(principal_header)
        user = self._users.get_by_principal(principal)
        if user is None and principal.is_swa_admin:
            user, _ = self._users.create_admin(principal)
        if user is None:
            raise RegistrationRequiredError("Auspex registration is required")
        return user

    def product_user(self, principal_header) -> AppUser:
        user = self.me(principal_header)
        if user.status != "active" or user.role not in {"user", "admin"}:
            raise AuthorizationError("Active Auspex user or admin role is required")
        return user

    def onboard(self, principal_header, payload) -> AppUser:
        user = self.product_user(principal_header)
        profile = OnboardingProfile.from_payload(payload)
        try:
            onboarded = user.complete_onboarding(
                profile,
                now=self._clock() if self._clock else None,
            )
        except ValueError as exc:
            raise AuthorizationError(str(exc)) from exc
        return self._users.replace(onboarded)

    def register(self, principal_header, payload):
        principal = self._principal(principal_header)
        acknowledgments = RegistrationAcknowledgments.from_payload(payload)
        acknowledgments.require_all()
        return self._users.create_pending(principal, acknowledgments, DOCUMENT_VERSIONS)

    def list_registrations(self, principal_header, status="pending"):
        self._admin(principal_header)
        if status not in {"pending", "active", "rejected", "suspended"}:
            raise ValueError("invalid registration status")
        return self._users.list_by_status(status)

    def review_user(self, principal_header, target_user_sk, action, note=None):
        _, admin = self._admin(principal_header)
        target = self._users.get_by_user_sk(target_user_sk)
        if target is None:
            raise UserNotFoundError("app_user was not found")
        try:
            reviewed = target.review(action, admin.user_sk, note=note)
        except ValueError as exc:
            raise InvalidTransitionError(str(exc)) from exc
        return self._users.replace(reviewed)
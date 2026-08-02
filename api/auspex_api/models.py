from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import uuid


_USER_NAMESPACE = uuid.UUID("b7301e2f-0b55-49e4-91bd-9dfdc2ae73e7")


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    identity_provider: str
    provider_user_id: str
    user_details: str | None = None
    swa_roles: frozenset[str] = frozenset()

    @property
    def identity_key(self) -> str:
        identity = f"{self.identity_provider}\0{self.provider_user_id}".encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    @property
    def user_sk(self) -> str:
        return str(uuid.uuid5(_USER_NAMESPACE, self.identity_key))

    @property
    def is_swa_admin(self) -> bool:
        return "admin" in self.swa_roles


@dataclass(frozen=True)
class RegistrationAcknowledgments:
    adult_confirmed: bool
    risk_disclosure_accepted: bool
    advisory_disclaimer_accepted: bool
    terms_accepted: bool
    privacy_acknowledged: bool

    @classmethod
    def from_payload(cls, payload: dict) -> "RegistrationAcknowledgments":
        if not isinstance(payload, dict):
            raise ValueError("registration payload must be an object")
        return cls(**{
            field: payload.get(field) is True
            for field in cls.__dataclass_fields__
        })

    def require_all(self) -> None:
        missing = [name for name, accepted in self.__dict__.items() if not accepted]
        if missing:
            raise ValueError(
                "All registration acknowledgments are required: " + ", ".join(missing)
            )


@dataclass(frozen=True)
class OnboardingProfile:
    risk_profile: str
    base_currency: str
    investment_horizon: str

    @classmethod
    def from_payload(cls, payload: dict) -> "OnboardingProfile":
        if not isinstance(payload, dict):
            raise ValueError("onboarding payload must be an object")
        if payload.get("suitability_acknowledged") is not True:
            raise ValueError("suitability acknowledgment is required")

        risk_profile = payload.get("risk_profile")
        base_currency = payload.get("base_currency")
        investment_horizon = payload.get("investment_horizon")
        if risk_profile not in {"Conservative", "Balanced", "Growth", "Aggressive"}:
            raise ValueError("invalid risk_profile")
        if base_currency not in {"USD", "CHF", "EUR"}:
            raise ValueError("invalid base_currency")
        if investment_horizon not in {"short", "12m", "long"}:
            raise ValueError("invalid investment_horizon")
        return cls(
            risk_profile=risk_profile,
            base_currency=base_currency,
            investment_horizon=investment_horizon,
        )


@dataclass(frozen=True)
class AppUser:
    user_sk: str
    identity_key: str
    identity_provider: str
    provider_user_id: str
    provider_user_details: str | None
    contact_email: str | None
    status: str
    role: str | None
    onboarded: bool
    base_currency: str
    risk_profile: str | None
    investment_horizon: str | None
    suitability_acknowledged_at: str | None
    adult_declaration_version: str | None
    adult_confirmed_at: str | None
    risk_disclosure_version: str | None
    risk_disclosure_accepted_at: str | None
    advisory_disclaimer_version: str | None
    advisory_disclaimer_accepted_at: str | None
    terms_version: str | None
    terms_accepted_at: str | None
    privacy_version: str | None
    privacy_acknowledged_at: str | None
    created_at: str
    updated_at: str
    reviewed_at: str | None
    reviewed_by_user_sk: str | None
    review_note: str | None
    review_history: tuple[dict, ...]

    @classmethod
    def pending_registration(
        cls,
        principal: AuthenticatedPrincipal,
        acknowledgments: RegistrationAcknowledgments,
        versions: dict[str, str],
        now: datetime | None = None,
    ) -> "AppUser":
        acknowledgments.require_all()
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        return cls(
            user_sk=principal.user_sk,
            identity_key=principal.identity_key,
            identity_provider=principal.identity_provider,
            provider_user_id=principal.provider_user_id,
            provider_user_details=principal.user_details,
            contact_email=principal.user_details if principal.user_details and "@" in principal.user_details else None,
            status="pending",
            role=None,
            onboarded=False,
            base_currency="USD",
            risk_profile=None,
            investment_horizon=None,
            suitability_acknowledged_at=None,
            adult_declaration_version=versions["adult_declaration"],
            adult_confirmed_at=timestamp,
            risk_disclosure_version=versions["risk_disclosure"],
            risk_disclosure_accepted_at=timestamp,
            advisory_disclaimer_version=versions["advisory_disclaimer"],
            advisory_disclaimer_accepted_at=timestamp,
            terms_version=versions["terms"],
            terms_accepted_at=timestamp,
            privacy_version=versions["privacy"],
            privacy_acknowledged_at=timestamp,
            created_at=timestamp,
            updated_at=timestamp,
            reviewed_at=None,
            reviewed_by_user_sk=None,
            review_note=None,
            review_history=(),
        )

    @classmethod
    def bootstrap_admin(cls, principal: AuthenticatedPrincipal, now=None) -> "AppUser":
        if not principal.is_swa_admin:
            raise ValueError("SWA admin role is required")
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        event = {
            "reviewed_at": timestamp,
            "reviewed_by_user_sk": principal.user_sk,
            "action": "bootstrap_admin",
            "previous_status": None,
            "new_status": "active",
            "note": "Bootstrapped from trusted SWA admin role",
        }
        return cls(
            user_sk=principal.user_sk,
            identity_key=principal.identity_key,
            identity_provider=principal.identity_provider,
            provider_user_id=principal.provider_user_id,
            provider_user_details=principal.user_details,
            contact_email=principal.user_details if principal.user_details and "@" in principal.user_details else None,
            status="active",
            role="admin",
            onboarded=False,
            base_currency="USD",
            risk_profile=None,
            investment_horizon=None,
            suitability_acknowledged_at=None,
            adult_declaration_version=None,
            adult_confirmed_at=None,
            risk_disclosure_version=None,
            risk_disclosure_accepted_at=None,
            advisory_disclaimer_version=None,
            advisory_disclaimer_accepted_at=None,
            terms_version=None,
            terms_accepted_at=None,
            privacy_version=None,
            privacy_acknowledged_at=None,
            created_at=timestamp,
            updated_at=timestamp,
            reviewed_at=timestamp,
            reviewed_by_user_sk=principal.user_sk,
            review_note=event["note"],
            review_history=(event,),
        )

    @classmethod
    def from_document(cls, document: dict) -> "AppUser":
        values = {
            field: document.get(field)
            for field in cls.__dataclass_fields__
        }
        values["review_history"] = tuple(values.get("review_history") or ())
        return cls(**values)

    def to_document(self) -> dict:
        document = {field: getattr(self, field) for field in self.__dataclass_fields__}
        document.update({
            "id": self.identity_key,
            "review_history": list(self.review_history),
            "schema_version": 3,
        })
        return document

    def public_profile(self) -> dict:
        capabilities = []
        if self.status == "active" and self.role in {"user", "admin"}:
            capabilities.append("product")
        if self.status == "active" and self.role == "admin":
            capabilities.append("admin")
        return {
            "user_sk": self.user_sk,
            "contact_email": self.contact_email,
            "status": self.status,
            "role": self.role,
            "onboarded": self.onboarded,
            "base_currency": self.base_currency,
            "risk_profile": self.risk_profile,
            "investment_horizon": self.investment_horizon,
            "suitability_acknowledged_at": self.suitability_acknowledged_at,
            "capabilities": capabilities,
        }

    def complete_onboarding(
        self,
        profile: OnboardingProfile,
        now: datetime | None = None,
    ) -> "AppUser":
        if self.status != "active" or self.role not in {"user", "admin"}:
            raise ValueError("active Auspex user or admin role is required")
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        return replace(
            self,
            onboarded=True,
            risk_profile=profile.risk_profile,
            base_currency=profile.base_currency,
            investment_horizon=profile.investment_horizon,
            suitability_acknowledged_at=timestamp,
            updated_at=timestamp,
        )

    def review(self, action: str, reviewer_user_sk: str, note=None, now=None) -> "AppUser":
        transitions = {
            "approve": ({"pending"}, "active", "user"),
            "reject": ({"pending"}, "rejected", None),
            "suspend": ({"active"}, "suspended", self.role),
            "restore": ({"suspended"}, "active", self.role),
        }
        if action not in transitions:
            raise ValueError(f"unsupported review action: {action}")
        allowed, new_status, new_role = transitions[action]
        if self.status not in allowed:
            raise ValueError(f"cannot {action} user with status {self.status}")
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        event = {
            "reviewed_at": timestamp,
            "reviewed_by_user_sk": reviewer_user_sk,
            "action": action,
            "previous_status": self.status,
            "new_status": new_status,
            "note": note,
        }
        return replace(
            self,
            status=new_status,
            role=new_role,
            updated_at=timestamp,
            reviewed_at=timestamp,
            reviewed_by_user_sk=reviewer_user_sk,
            review_note=note,
            review_history=self.review_history + (event,),
        )
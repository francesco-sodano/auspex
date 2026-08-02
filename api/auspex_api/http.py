from dataclasses import dataclass
import logging
from typing import Callable

from .auth import AuthenticationError
from .models import AppUser
from .services import (
    AuthorizationError,
    InvalidTransitionError,
    RegistrationRequiredError,
    UserNotFoundError,
)


@dataclass(frozen=True)
class HttpResult:
    payload: dict
    status_code: int


def execute(operation: Callable[[], object], success_status: int = 200) -> HttpResult:
    try:
        return HttpResult(operation(), success_status)
    except AuthenticationError as exc:
        return HttpResult({"error": "unauthorized", "message": str(exc)}, 401)
    except RegistrationRequiredError as exc:
        return HttpResult({"error": "registration_required", "message": str(exc)}, 404)
    except AuthorizationError as exc:
        return HttpResult({"error": "forbidden", "message": str(exc)}, 403)
    except UserNotFoundError as exc:
        return HttpResult({"error": "not_found", "message": str(exc)}, 404)
    except InvalidTransitionError as exc:
        return HttpResult({"error": "invalid_transition", "message": str(exc)}, 409)
    except ValueError as exc:
        return HttpResult({"error": "invalid_request", "message": str(exc)}, 400)
    except Exception:
        logging.exception("E19 request failed")
        return HttpResult({"error": "internal_error"}, 500)


def registration_payload(user: AppUser) -> dict:
    return {
        **user.public_profile(),
        "created_at": user.created_at,
        "reviewed_at": user.reviewed_at,
        "review_note": user.review_note,
    }
from pathlib import Path
import unittest

from api.auspex_api.auth import AuthenticationError
from api.auspex_api.http import execute
from api.auspex_api.services import (
    AuthorizationError,
    InvalidTransitionError,
    RegistrationRequiredError,
    UserNotFoundError,
)


ROOT = Path(__file__).resolve().parents[1]


class E19FunctionAppTests(unittest.TestCase):
    def test_admin_routes_avoid_reserved_admin_path(self):
        function_app = (ROOT / "api" / "function_app.py").read_text(encoding="utf-8")

        for route in [
            'route="registration_queue"',
            'route="approve_registration/{user_sk}"',
            'route="reject_registration/{user_sk}"',
            'route="suspend_user/{user_sk}"',
            'route="restore_user/{user_sk}"',
        ]:
            self.assertIn(route, function_app)
        self.assertNotIn('route="admin/', function_app)
        self.assertNotIn('route="admin_', function_app)

    def test_execute_maps_success(self):
        result = execute(lambda: {"status": "pending"}, success_status=201)

        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.payload["status"], "pending")

    def test_execute_maps_expected_security_and_state_errors(self):
        cases = [
            (AuthenticationError("bad principal"), 401, "unauthorized"),
            (RegistrationRequiredError("register"), 404, "registration_required"),
            (AuthorizationError("admin only"), 403, "forbidden"),
            (UserNotFoundError("missing"), 404, "not_found"),
            (InvalidTransitionError("bad state"), 409, "invalid_transition"),
            (ValueError("bad input"), 400, "invalid_request"),
        ]

        for error, expected_status, expected_code in cases:
            with self.subTest(error=error):
                def operation(error=error):
                    raise error
                result = execute(operation)
                self.assertEqual(result.status_code, expected_status)
                self.assertEqual(result.payload["error"], expected_code)


if __name__ == "__main__":
    unittest.main()
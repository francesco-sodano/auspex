from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

CONNECTORS_ROOT = Path(__file__).resolve().parents[1] / "connectors"
sys.path.insert(0, str(CONNECTORS_ROOT))

from tests import test_connectors  # noqa: F401 - installs optional dependency stubs
from shared.retry import _retry_delay


class RetryDelayTests(unittest.TestCase):
    def test_numeric_retry_after_is_honored_with_bounded_jitter(self):
        with patch("shared.retry.random.uniform", return_value=0.25):
            self.assertEqual(_retry_delay("5", 1), 5.25)

    def test_http_date_retry_after_is_supported(self):
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        with patch("shared.retry.random.uniform", return_value=0):
            delay = _retry_delay(format_datetime(retry_at, usegmt=True), 1)
        self.assertGreaterEqual(delay, 28)
        self.assertLessEqual(delay, 31)

    def test_invalid_retry_after_uses_exponential_fallback(self):
        with patch("shared.retry.random.uniform", return_value=0):
            self.assertEqual(_retry_delay("not-a-date", 3), 8)

    def test_explicit_retry_after_is_not_shortened(self):
        with patch("shared.retry.random.uniform", return_value=0):
            self.assertEqual(_retry_delay("600", 1), 600)


if __name__ == "__main__":
    unittest.main()
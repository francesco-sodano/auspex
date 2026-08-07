import json
import unittest

from engine.company_narrative import (
    PROMPT_VERSION,
    narrative_cache_key,
    parse_company_narrative,
)
from tests.test_company_package import package


def response(citation_id="document-1"):
    claim = {"text": "Current evidence supports the stated direction.", "citation_ids": [citation_id]}
    return {
        "outlook_direction": "ACCELERATING",
        "outlook_horizon_days": 90,
        "summary": claim,
        "thesis": claim,
        "positive_catalysts": [claim],
        "negative_catalysts": [],
        "risks": [claim],
        "invalidators": [claim],
        "uncertainty": claim,
    }


class CompanyNarrativeTests(unittest.TestCase):
    def test_narrative_is_bound_to_package_direction_and_citations(self):
        current = package()

        narrative = parse_company_narrative(
            json.dumps(response()),
            current,
            model_version="gpt-4o:2024-11-20",
        )

        self.assertEqual(narrative.outlook_direction, current.outlook_direction)
        self.assertEqual(narrative.summary.citation_ids, ("document-1",))
        self.assertEqual(narrative.prompt_version, PROMPT_VERSION)

    def test_narrative_cannot_change_deterministic_direction(self):
        current = package()
        payload = response()
        payload["outlook_direction"] = "DETERIORATING"

        with self.assertRaisesRegex(ValueError, "cannot alter"):
            parse_company_narrative(
                json.dumps(payload),
                current,
                model_version="gpt-4o:2024-11-20",
            )

    def test_every_claim_requires_known_package_evidence(self):
        with self.assertRaisesRegex(ValueError, "unknown package evidence"):
            parse_company_narrative(
                json.dumps(response("unknown-document")),
                package(),
                model_version="gpt-4o:2024-11-20",
            )

    def test_cache_key_changes_with_package_or_prompt_version(self):
        first = narrative_cache_key("a" * 64, "model-1")
        changed_package = narrative_cache_key("b" * 64, "model-1")
        changed_prompt = narrative_cache_key("a" * 64, "model-1", "prompt-2")

        self.assertNotEqual(first, changed_package)
        self.assertNotEqual(first, changed_prompt)


if __name__ == "__main__":
    unittest.main()
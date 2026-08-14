import json

from auspex.extraction.channel_b import ChannelBExtractor


def test_unknown_comparative_values_are_sanitized():
    extractor = ChannelBExtractor(
        openai_client=None,
        deployment="test",
        system_prompt="test",
        model_version="test",
        sink=None,
    )
    digest = extractor.parse_response(
        json.dumps(
            {
                "headline": "Update",
                "digest": "Evidence.",
                "comparative": {
                    "comparative_summary": "extra",
                    "risk_factors_added": [
                        {
                            "summary": "Technology risk",
                            "verbatim": "Evidence",
                            "category": "TECHNOLOGY",
                            "severity": "UNKNOWN",
                        }
                    ],
                    "guidance_language_shift": "UNKNOWN",
                    "mda_tone_shift": "UNKNOWN",
                },
            }
        ),
        security_id="sec-1",
        document_id="doc-1",
        content_hash="hash",
    )

    risk = digest.comparative.risk_factors_added[0]
    assert risk.category.value == "OTHER"
    assert risk.severity.value == "LOW"
    assert digest.comparative.guidance_language_shift.value == "UNCHANGED"
    assert digest.comparative.mda_tone_shift.value == "UNCHANGED"

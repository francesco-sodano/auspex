import json

from auspex.extraction.channel_a import ChannelAExtractor


def test_invalid_model_enums_default_or_drop():
    extractor = ChannelAExtractor(
        openai_client=None,
        deployment="test",
        system_prompt="test",
        model_version="test",
        taxonomy_version="test",
        sink=None,
    )
    extraction = extractor.parse_response(
        json.dumps(
            {
                "materiality": "UNKNOWN",
                "sentiment": "UNKNOWN",
                "guidance_direction": "UNKNOWN",
                "novelty": "UNKNOWN",
                "extraction_confidence": "UNKNOWN",
                "theme_claims": [
                    {
                        "theme_id": "valid",
                        "strength": "STRONG",
                        "evidence_excerpt": "evidence",
                    },
                    {
                        "theme_id": "invalid",
                        "strength": "OTHER",
                        "evidence_excerpt": "evidence",
                    },
                ],
                "risk_claims": [],
                "narrative_claims": [
                    {
                        "claim_type": "NEW_PRODUCT",
                        "strength": "STRONG",
                        "evidence_excerpt": "evidence",
                        "location_hint": "extra field",
                    }
                ],
            }
        ),
        security_id="sec-1",
        document_id="doc-1",
        content_hash="hash",
    )

    assert extraction.materiality.value == "NONE"
    assert extraction.sentiment.value == "NEUTRAL"
    assert extraction.guidance_direction.value == "NONE"
    assert extraction.novelty.value == "ROUTINE"
    assert extraction.extraction_confidence.value == "LOW"
    assert len(extraction.theme_claims) == 1
    assert len(extraction.narrative_claims) == 1

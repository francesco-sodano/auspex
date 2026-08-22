import json

import pytest

from auspex.extraction.channel_b import ChannelBExtractor
from auspex.extraction.sections import Section


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
                "plain_summary": "A plain update for a new reader.",
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
    assert digest.plain_summary == "A plain update for a new reader."
    assert digest.comparative.guidance_language_shift.value == "UNCHANGED"
    assert digest.comparative.mda_tone_shift.value == "UNCHANGED"


class _OpenAI:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def complete_json(self, **_kwargs) -> str:
        return json.dumps(self.payload)


class _Sink:
    def __init__(self) -> None:
        self.stored = None

    async def find_by_cache_key(self, _cache_key):
        return None

    async def upsert(self, digest):
        self.stored = digest


@pytest.mark.asyncio
async def test_extract_keeps_only_source_verified_summary_evidence_and_quotes():
    source = (
        "Revenue increased  by 16 percent.\n"
        "Management expanded capacity."
    )
    sink = _Sink()
    extractor = ChannelBExtractor(
        openai_client=_OpenAI(
            {
                "headline": "Growth update",
                "plain_summary": "Revenue increased while capacity expanded.",
                "plain_summary_evidence": [
                    "Revenue increased by 16 percent.",
                    "This sentence is not in the source.",
                ],
                "digest": "The company reported growth and investment.",
                "key_quotes": [
                    {
                        "text": "Management expanded capacity.",
                        "section": "current",
                        "why_it_matters": "More room for growth.",
                    },
                    {
                        "text": "Invented quote.",
                        "section": "current",
                        "why_it_matters": "It should be removed.",
                    },
                ],
            }
        ),
        deployment="test",
        system_prompt="test",
        model_version="test",
        sink=sink,
    )

    digest = await extractor.extract(
        security_id="sec-1",
        document_id="doc-1",
        content_hash="hash",
        ticker="TEST",
        form_type="10-Q",
        sections=[Section(item="current", text=source)],
    )

    assert digest.plain_summary == "Revenue increased while capacity expanded."
    assert digest.plain_summary_evidence == [
        "Revenue increased by 16 percent."
    ]
    assert [quote.text for quote in digest.key_quotes] == [
        "Management expanded capacity."
    ]
    assert sink.stored == digest


@pytest.mark.asyncio
async def test_extract_removes_plain_summary_without_verified_source_excerpt():
    sink = _Sink()
    extractor = ChannelBExtractor(
        openai_client=_OpenAI(
            {
                "headline": "Unsupported update",
                "plain_summary": "A claim unsupported by the source.",
                "plain_summary_evidence": ["Not present."],
                "digest": "Detailed prose.",
            }
        ),
        deployment="test",
        system_prompt="test",
        model_version="test",
        sink=sink,
    )

    digest = await extractor.extract(
        security_id="sec-1",
        document_id="doc-1",
        content_hash="hash",
        ticker="TEST",
        form_type="8-K",
        sections=[Section(item="current", text="Only the filed source text.")],
    )

    assert digest.plain_summary is None
    assert digest.plain_summary_evidence == []

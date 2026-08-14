import json

from auspex.extraction.channel_a import ChannelAExtractor
from auspex.extraction.channel_b import ChannelBExtractor
from auspex.extraction.sections import Section


def test_channel_a_payload_is_bounded():
    extractor = ChannelAExtractor(
        openai_client=None,
        deployment="test",
        system_prompt="test",
        model_version="test",
        taxonomy_version="test",
        sink=None,
    )
    payload = json.loads(
        extractor.build_user_content(
            ticker="TEST",
            form_type="10-K",
            sections=[
                Section(item="one", text="x" * 200_000),
                Section(item="two", text="y" * 200_000),
            ],
            taxonomy_theme_ids=[],
        )
    )
    assert sum(len(section["text"]) for section in payload["sections"]) == 300_000


def test_channel_b_splits_budget_between_current_and_prior():
    extractor = ChannelBExtractor(
        openai_client=None,
        deployment="test",
        system_prompt="test",
        model_version="test",
        sink=None,
    )
    payload = json.loads(
        extractor.build_user_content(
            ticker="TEST",
            form_type="10-K",
            sections=[Section(item="current", text="x" * 200_000)],
            prior_sections=[Section(item="prior", text="y" * 200_000)],
        )
    )
    assert len(payload["sections"][0]["text"]) == 150_000
    assert len(payload["prior_document"]["sections"][0]["text"]) == 150_000

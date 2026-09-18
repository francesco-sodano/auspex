import json
from unittest.mock import AsyncMock

import pytest

from auspex.models.extraction import ChannelBDigest
from auspex.narrative.generator import MAX_NARRATIVE_INPUT_CHARS, NarrativeGenerator


def _generator():
    return NarrativeGenerator(
        openai_client=AsyncMock(), deployment="test", system_prompt="Explain source facts.",
        model_version="test", sink=AsyncMock(),
    )


def test_large_digest_bundle_is_bounded_without_changing_authoritative_reasons():
    package = {
        "security_id": "issuer",
        "leg_explanations": {"smart_money": {"summary": "Officers reported share sales.", "effect": "weighs"}},
    }
    digests = [
        ChannelBDigest(
            id=f"digest-{index}", security_id="issuer", document_id=f"doc-{index}",
            content_hash=f"hash-{index}", model_version="test",
            headline="Long headline " * 100, digest="Long secondary source detail. " * 2000,
        )
        for index in range(20)
    ]
    content = _generator().build_user_content(
        package=package, leg_changes=[], digests=digests, comparative=None
    )
    payload = json.loads(content)
    assert len(content) <= MAX_NARRATIVE_INPUT_CHARS
    assert payload["package"] == package
    assert payload["context_limited"] is True
    assert len(payload["digests"]) == 8
    assert all(len(digest["summary"]) <= 1200 for digest in payload["digests"])
    assert "created_at" not in content


def test_authoritative_package_is_never_silently_truncated():
    with pytest.raises(ValueError, match="authoritative score package"):
        _generator().build_user_content(
            package={"security_id": "issuer", "leg_explanations": {"summary": "fact " * 6000}},
            leg_changes=[], digests=[], comparative=None,
        )


def test_small_context_is_not_reported_as_limited():
    payload = json.loads(_generator().build_user_content(
        package={"security_id": "issuer"}, leg_changes=[], digests=[], comparative=None
    ))
    assert payload["context_limited"] is False

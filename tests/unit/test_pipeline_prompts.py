"""Unit tests for versioned system-prompt loading (arc42 §5.4, §5.9)."""

from __future__ import annotations

import pytest

from auspex.pipeline.prompts import UnknownPromptVersionError, load_prompt


class TestLoadPrompt:
    def test_loads_extract_channel_a_prompt_from_disk(self):
        text = load_prompt("extract-a-v1")
        assert "Channel A" in text
        assert "prompt_version: extract-a-v1" in text

    def test_loads_extract_channel_b_prompt_from_disk(self):
        text = load_prompt("digest-b-v1")
        assert "prompt_version: digest-b-v1" in text

    def test_loads_narrative_prompt_from_disk(self):
        text = load_prompt("narrative-v1")
        assert "prompt_version: narrative-v1" in text

    def test_loads_planner_prompt_from_disk(self):
        text = load_prompt("planner-v1")
        assert "prompt_version: planner-v1" in text

    def test_loads_answer_prompt_from_disk(self):
        text = load_prompt("answer-v1")
        assert "prompt_version: answer-v1" in text

    def test_result_is_cached_across_calls(self):
        first = load_prompt("extract-a-v1")
        second = load_prompt("extract-a-v1")
        assert first is second  # lru_cache returns the exact same str object

    def test_unknown_prompt_version_raises(self):
        with pytest.raises(UnknownPromptVersionError):
            load_prompt("no-such-prompt-version")

"""Versioned system-prompt loading (arc42 §5.4, §5.9, TC — prompts are
config, not code).

Every LLM call's system prompt is loaded verbatim from ``prompts/*.md``
(``Settings.prompts_dir``) and keyed by the same ``prompt_version`` string
the caller's extractor/generator class already carries
(:attr:`auspex.extraction.channel_a.ChannelAExtractor.prompt_version`,
:attr:`auspex.extraction.channel_b.ChannelBExtractor.prompt_version`,
:attr:`auspex.narrative.generator.NarrativeGenerator.prompt_version`, ...),
so bumping a prompt file's content without bumping its ``prompt_version``
constant is caught by cache-key/prompt drift rather than silently served
stale — the extraction/narrative cache keys already derive from
``prompt_version`` (:mod:`auspex.extraction.cache`,
:func:`auspex.narrative.fingerprint.compute_package_fingerprint`), so a
version bump here (and to the filename below) is what actually invalidates
those caches.
"""

from __future__ import annotations

from functools import lru_cache

from auspex.settings import get_settings

# prompt_version -> markdown filename under `Settings.prompts_dir`. The
# filenames predate the `-`-separated `prompt_version` strings used in code
# (`prompts/extract_channel_a_v1.md` vs. `"extract-a-v1"`), so the mapping
# is explicit rather than derived from the version string.
_PROMPT_FILES: dict[str, str] = {
    "extract-a-v1": "extract_channel_a_v1.md",
    "digest-b-v1": "extract_channel_b_v1.md",
    "narrative-v1": "narrative_v1.md",
    "planner-v1": "planner_v1.md",
    "answer-v1": "answer_v1.md",
}


class UnknownPromptVersionError(KeyError):
    """Raised when no prompt file is registered for a given ``prompt_version``."""


@lru_cache
def load_prompt(prompt_version: str) -> str:
    """Load the versioned system prompt text for ``prompt_version``.

    Cached per-process (prompt files never change mid-run — a fresh
    process/container revision re-reads disk, e.g. after a prompt file is
    edited and redeployed, without needing an explicit cache-bust).
    """

    filename = _PROMPT_FILES.get(prompt_version)
    if filename is None:
        raise UnknownPromptVersionError(f"no prompt file registered for prompt_version={prompt_version!r}")
    path = get_settings().prompts_dir / filename
    return path.read_text(encoding="utf-8")

"""Extraction cache key (arc42 §5.4 "Cache key").

```
security_id + content_hash + model_version + prompt_version + schema_version + taxonomy_version
```

Unchanged content at unchanged versions is never re-read. Changing the
prompt, schema, or theme taxonomy invalidates the cache and triggers
controlled re-extraction.
"""

from __future__ import annotations


def channel_a_cache_key(
    *,
    security_id: str,
    content_hash: str,
    model_version: str,
    prompt_version: str = "extract-a-v1",
    schema_version: str = "4.0",
    taxonomy_version: str,
) -> str:
    return "|".join(
        [
            security_id,
            content_hash,
            model_version,
            prompt_version,
            schema_version,
            taxonomy_version,
        ]
    )


def channel_b_cache_key(
    *,
    security_id: str,
    content_hash: str,
    model_version: str,
    prompt_version: str = "digest-b-v2",
) -> str:
    return "|".join(
        [security_id, content_hash, model_version, prompt_version]
    )

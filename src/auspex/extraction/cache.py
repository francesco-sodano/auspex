"""Extraction cache key (arc42 §5.4 "Cache key").

```
security_id + content_hash + model_version + prompt_version + schema_version + taxonomy_version
```

Unchanged content at unchanged versions is never re-read. Changing the
prompt, schema, or theme taxonomy invalidates the cache and triggers
controlled re-extraction.
"""

from __future__ import annotations

import json

from auspex.models.common import sha256_hex


def extraction_input_fingerprint(system_prompt: str, user_content: str) -> str:
    return sha256_hex(json.dumps(
        {"system_prompt": system_prompt, "user_content": user_content},
        sort_keys=True,
        ensure_ascii=False,
    ))


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

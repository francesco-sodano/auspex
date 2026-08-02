"""HTTP retry helper — exponential backoff with Retry-After support."""
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

import httpx

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_TIMEOUT = 30.0


def _retry_delay(retry_after: str, attempt: int) -> float:
    fallback = float(2**attempt)
    explicit_delay = False
    if retry_after:
        try:
            base_delay = max(0.0, float(retry_after))
            explicit_delay = True
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                base_delay = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                explicit_delay = True
            except (TypeError, ValueError, OverflowError):
                base_delay = fallback
    else:
        base_delay = fallback
    delay = base_delay + random.uniform(0.0, min(1.0, base_delay * 0.1))
    return delay if explicit_delay else min(120.0, delay)


def http_get(
    url: str,
    params: dict = None,
    headers: dict = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    timeout: float = _DEFAULT_TIMEOUT,
    max_response_bytes: int = None,
    before_attempt: Optional[Callable[[], None]] = None,
) -> httpx.Response:
    for attempt in range(1, max_attempts + 1):
        if before_attempt is not None:
            before_attempt()
        try:
            if max_response_bytes is None:
                resp = httpx.get(url, params=params, headers=headers, timeout=timeout)
            else:
                with httpx.stream(url=url, method="GET", params=params, headers=headers, timeout=timeout) as streamed:
                    if streamed.status_code == 429:
                        wait = _retry_delay(streamed.headers.get("Retry-After"), attempt)
                        time.sleep(wait)
                        continue
                    if streamed.status_code >= 500 and attempt < max_attempts:
                        time.sleep(_retry_delay(None, attempt))
                        continue
                    streamed.raise_for_status()

                    content = bytearray()
                    truncated = False
                    for chunk in streamed.iter_bytes():
                        remaining = max_response_bytes + 1 - len(content)
                        if remaining <= 0:
                            truncated = True
                            break
                        content.extend(chunk[:remaining])
                        if len(content) > max_response_bytes:
                            truncated = True
                            break

                    bounded_content = bytes(content[:max_response_bytes])
                    content_length = streamed.headers.get("Content-Length")
                    if content_length and int(content_length) > len(bounded_content):
                        truncated = True
                    content_range = streamed.headers.get("Content-Range", "")
                    if "/" in content_range:
                        total = content_range.rsplit("/", 1)[-1]
                        if total.isdigit() and int(total) > len(bounded_content):
                            truncated = True

                    extensions = dict(streamed.extensions)
                    extensions["auspex_truncated"] = truncated
                    return httpx.Response(
                        status_code=streamed.status_code,
                        headers=streamed.headers,
                        content=bounded_content,
                        request=streamed.request,
                        extensions=extensions,
                    )
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == max_attempts:
                raise
            time.sleep(_retry_delay(None, attempt))
            continue

        if resp.status_code == 429:
            wait = _retry_delay(resp.headers.get("Retry-After"), attempt)
            time.sleep(wait)
            continue

        if resp.status_code >= 500 and attempt < max_attempts:
            time.sleep(_retry_delay(None, attempt))
            continue

        resp.raise_for_status()
        return resp

    raise RuntimeError(f"All {max_attempts} attempts failed for {url}")


def http_post(
    url: str,
    json: dict = None,
    headers: dict = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    timeout: float = _DEFAULT_TIMEOUT,
) -> httpx.Response:
    for attempt in range(1, max_attempts + 1):
        try:
            resp = httpx.post(url, json=json, headers=headers, timeout=timeout)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == max_attempts:
                raise
            time.sleep(_retry_delay(None, attempt))
            continue

        if resp.status_code == 429:
            wait = _retry_delay(resp.headers.get("Retry-After"), attempt)
            time.sleep(wait)
            continue

        if resp.status_code >= 500 and attempt < max_attempts:
            time.sleep(_retry_delay(None, attempt))
            continue

        resp.raise_for_status()
        return resp

    raise RuntimeError(f"All {max_attempts} attempts failed for {url}")

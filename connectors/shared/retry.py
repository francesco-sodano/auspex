"""HTTP retry helper — exponential backoff with Retry-After support."""
import time

import httpx

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_TIMEOUT = 30.0


def http_get(
    url: str,
    params: dict = None,
    headers: dict = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    timeout: float = _DEFAULT_TIMEOUT,
) -> httpx.Response:
    for attempt in range(1, max_attempts + 1):
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=timeout)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == max_attempts:
                raise
            time.sleep(2**attempt)
            continue

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2**attempt))
            time.sleep(wait)
            continue

        if resp.status_code >= 500 and attempt < max_attempts:
            time.sleep(2**attempt)
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
            time.sleep(2**attempt)
            continue

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2**attempt))
            time.sleep(wait)
            continue

        if resp.status_code >= 500 and attempt < max_attempts:
            time.sleep(2**attempt)
            continue

        resp.raise_for_status()
        return resp

    raise RuntimeError(f"All {max_attempts} attempts failed for {url}")

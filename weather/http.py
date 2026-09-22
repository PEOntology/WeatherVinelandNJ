"""Shared HTTP session with retries on transient failures."""
from __future__ import annotations

import time

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}


class SourceError(RuntimeError):
    """A data source could not be read. Callers must record 'unavailable', never zero."""


def request(method: str, url: str, *, attempts: int = 4, timeout: float = 60, **kw) -> requests.Response:
    last: Exception | None = None
    for i in range(attempts):
        try:
            resp = requests.request(method, url, timeout=timeout, **kw)
            if resp.status_code in RETRY_STATUS and i < attempts - 1:
                time.sleep(2 ** (i + 1))
                continue
            return resp
        except requests.RequestException as exc:  # network error
            last = exc
            if i < attempts - 1:
                time.sleep(2 ** (i + 1))
    raise SourceError(f"{method} {url.split('?')[0]} failed: {last}")


def get_json(url: str, **kw) -> dict:
    resp = request("GET", url, **kw)
    if resp.status_code != 200:
        raise SourceError(f"GET {url.split('?')[0]} -> HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()

"""Access gate for the personal panel and its HTTP API."""
from __future__ import annotations

import os
from urllib.parse import urlparse


def requires_auth() -> bool:
    return os.environ.get("RENDER", "").casefold() == "true" or bool(os.environ.get("PANEL_PASSWORD"))


def configured() -> bool:
    return bool(os.environ.get("PANEL_PASSWORD"))


def same_origin_post(
    origin: str | None,
    host: str | None,
    fetch_site: str | None,
    referer: str | None = None,
    scheme: str = "http",
) -> bool:
    site = (fetch_site or "").casefold()
    if site == "same-origin":
        # The browser determines this relative to the public URL, before a
        # reverse proxy can rewrite Host or the request scheme.
        return True
    if site in {"cross-site", "same-site"}:
        return False
    source = origin or referer
    if not source:
        return False
    parsed = urlparse(source)
    expected_scheme = "https" if os.environ.get("RENDER", "").casefold() == "true" else scheme
    return parsed.scheme == expected_scheme and parsed.netloc.casefold() == (host or "").casefold()

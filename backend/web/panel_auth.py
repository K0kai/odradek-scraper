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
    if fetch_site and fetch_site.casefold() != "same-origin":
        return False
    source = origin or referer
    if not source:
        return (fetch_site or "").casefold() == "same-origin"
    parsed = urlparse(source)
    expected_scheme = "https" if os.environ.get("RENDER", "").casefold() == "true" else scheme
    return parsed.scheme == expected_scheme and parsed.netloc.casefold() == (host or "").casefold()

"""Access gate for the personal panel and its HTTP API."""
from __future__ import annotations

import os
from urllib.parse import urlparse


def requires_auth() -> bool:
    return os.environ.get("RENDER", "").casefold() == "true" or bool(os.environ.get("PANEL_PASSWORD"))


def configured() -> bool:
    return bool(os.environ.get("PANEL_PASSWORD"))


def same_origin_post(origin: str | None, host: str | None, fetch_site: str | None) -> bool:
    if (fetch_site or "").casefold() == "cross-site":
        return False
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == (host or "").casefold()

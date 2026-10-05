"""HTMX web presentation layer."""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Lazily expose the web application factory (`covenant_radar.web.app`)."""
    from covenant_radar.web.app import create_app as factory

    return factory(*args, **kwargs)


__all__ = ["create_app"]

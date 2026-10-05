"""Covenant Radar ASGI entrypoint.

`uvicorn covenant_radar.asgi:app` serves the default application.  The factory
itself lives in the web layer (`covenant_radar.web.app`); it is re-exported here
for callers and tests that build their own app.
"""

from __future__ import annotations

from covenant_radar.web.app import _configure_template_environment, create_app, load_catalogue

app = create_app()
application = app


__all__ = ["_configure_template_environment", "app", "application", "create_app", "load_catalogue"]

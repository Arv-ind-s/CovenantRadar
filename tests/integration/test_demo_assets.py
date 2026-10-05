"""Presentation downloads retain the disabled-by-default permission boundary."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request

from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.demo_walkthrough import create_demo_walkthrough_router


def test_assets_require_demo_mode_authority_and_an_allowlisted_file(tmp_path):
    asset = tmp_path / "sanction-letter.pdf"
    asset.write_bytes(b"presentation fixture")
    (tmp_path / "private.txt").write_text("not downloadable")
    web = SimpleNamespace(demo_walkthrough_enabled=False, demo_assets_path=tmp_path)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(web=web))),
        }
    )
    router = create_demo_walkthrough_router(None, ingestion=None, nightly=None)
    handler = next(route.endpoint for route in router.routes if route.name == "demo_asset")
    authority = Principal.user(uuid4(), (Permission.VIEW_QUEUE, Permission.APPROVE_MODEL_PROMOTION))
    reader = Principal.user(uuid4(), (Permission.VIEW_QUEUE,))
    for enabled, principal, filename in (
        (False, authority, asset.name),
        (True, reader, asset.name),
        (True, authority, "private.txt"),
        (True, authority, "../private.txt"),
        (True, authority, "sanction-letter.docx"),
    ):
        web.demo_walkthrough_enabled = enabled
        with pytest.raises(HTTPException) as denied:
            handler(filename, request, principal)
        assert denied.value.status_code == 404
    response = handler(asset.name, request, authority)
    assert response.path == asset

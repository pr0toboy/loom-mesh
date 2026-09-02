"""The dashboard is served — the one promise a README cannot keep on its own.

The API mounts ``/ui`` only if it finds a directory to serve, and its default
used to point at a path no checkout contained. The service started, every API
route answered, and the interface the documentation described 404'd. Nothing in
the suite noticed, because nothing asked for a page.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_dashboard_is_served_from_a_fresh_checkout():
    from mesh_api.main import app

    with TestClient(app) as client:
        r = client.get("/ui/")
    assert r.status_code == 200, "the repository ships a dashboard; /ui must serve it"
    assert "<html" in r.text.lower()

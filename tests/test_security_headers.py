"""Tests for HTTP security hardening (headers, CORS, docs lockdown)."""

from fastapi.testclient import TestClient

from app.main import app


def test_security_headers_are_present_on_health():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in response.headers


def test_root_exposes_docs_link_in_development():
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["health"] == "/health"
    # Development defaults keep docs enabled.
    assert body.get("documentation") == "/docs"


def test_cors_preflight_allows_dashboard_origin():
    with TestClient(app) as client:
        response = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:8501",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code in {200, 204}
    assert (
        response.headers.get("access-control-allow-origin") == "http://localhost:8501"
    )

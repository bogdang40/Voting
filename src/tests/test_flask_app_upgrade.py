import importlib
import re
from pathlib import Path

import pytest


@pytest.fixture()
def app_module(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VOTES_DB_PATH", str(tmp_path / "votes-test.db"))
    monkeypatch.setenv("VOTES_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_ADMIN_USER", "admin")
    monkeypatch.setenv("APP_ADMIN_PASSWORD", "secret123")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-key")

    module = importlib.import_module("app")
    module = importlib.reload(module)
    module.app.config.update(TESTING=True)
    return module


@pytest.fixture()
def client(app_module):
    return app_module.app.test_client()


def extract_csrf(html: str) -> str:
    m = re.search(r'csrf-token" content="([^"]+)"', html)
    assert m, "CSRF token not found in response"
    return m.group(1)


def login(client):
    page = client.get("/login")
    token = extract_csrf(page.get_data(as_text=True))
    return client.post(
        "/login",
        data={
            "_csrf_token": token,
            "username": "admin",
            "password": "secret123",
        },
        follow_redirects=False,
    )


def test_mutation_requires_login(client):
    dashboard = client.get("/")
    token = extract_csrf(dashboard.get_data(as_text=True))

    response = client.post(
        "/new",
        data={
            "_csrf_token": token,
            "title": "Test Auth",
            "total_ballots": "10",
            "candidate_name": ["A", "B"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_and_create_instance_then_export_csv(client):
    auth = login(client)
    assert auth.status_code == 302

    page = client.get("/new")
    token = extract_csrf(page.get_data(as_text=True))

    create = client.post(
        "/new",
        data={
            "_csrf_token": token,
            "title": "Alegeri Test",
            "total_ballots": "20",
            "candidate_name": ["Ana", "Mihai"],
        },
        follow_redirects=False,
    )
    assert create.status_code == 302

    location = create.headers["Location"]
    assert location.endswith("/")
    slug = location.strip("/").split("/")[0]

    export = client.get(f"/{slug}/results/export.csv")
    assert export.status_code == 200
    body = export.get_data(as_text=True)
    assert "candidat,DA,NU,BLANK" in body


def test_scan_health_api_requires_auth_and_returns_payload(app_module, client):
    iid, slug = app_module.create_instance("API Test", 5, ["A", "B"])
    instance, candidates = app_module.get_instance(slug)
    app_module.generate_instance_files(instance, candidates)

    unauth = client.get(f"/api/{slug}/scan/health")
    assert unauth.status_code == 401

    login(client)
    auth = client.get(f"/api/{slug}/scan/health")
    assert auth.status_code == 200

    payload = auth.get_json()
    assert payload["instance"]["id"] == iid
    assert payload["features"]["guided_capture"] is True


def test_live_frame_check_api_returns_marker_payload(app_module, client):
    iid, slug = app_module.create_instance("Live API Test", 5, ["A", "B"])
    instance, candidates = app_module.get_instance(slug)
    app_module.generate_instance_files(instance, candidates)

    login(client)
    page = client.get(f"/{slug}/")
    token = extract_csrf(page.get_data(as_text=True))

    with open("ballot_sample_001.png", "rb") as f:
        response = client.post(
            f"/api/{slug}/scan/live-frame-check",
            data={"ballot": (f, "ballot_sample_001.png")},
            headers={"X-CSRF-Token": token},
            content_type="multipart/form-data",
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert "markers" in payload
    assert "quality" in payload
    assert "ready_to_capture" in payload
    assert isinstance(payload["markers"].get("all_found"), bool)


def test_low_confidence_requires_override(app_module, client):
    _iid, slug = app_module.create_instance("Override Rule", 5, ["A", "B"])
    instance, candidates = app_module.get_instance(slug)
    app_module.generate_instance_files(instance, candidates)

    login(client)
    page = client.get(f"/{slug}/")
    token = extract_csrf(page.get_data(as_text=True))

    with open("ballot_sample_001.png", "rb") as f:
        response = client.post(
            f"/{slug}/scan",
            data={
                "_csrf_token": token,
                "capture_mode": "manual_live",
                "capture_confidence": "20",
                "operator_override": "0",
                "ballot": (f, "ballot_sample_001.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/{slug}/")


def test_invalid_csrf_rejected_on_login_post(client):
    client.get("/login")
    response = client.post(
        "/login",
        data={
            "_csrf_token": "bad-token",
            "username": "admin",
            "password": "secret123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

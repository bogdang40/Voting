import importlib
import io
import json
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


def seed_scanned_ballot(app_module, slug: str, ballot_number: int, vote_values: list[str]):
    instance, candidates = app_module.get_instance(slug)
    votes = {
        candidate["field_id"]: vote_values[idx]
        for idx, candidate in enumerate(candidates)
    }
    app_module._save_ballot_to_db(
        instance,
        candidates,
        ballot_number,
        votes,
        f"instances/{slug}/scans/ballot_{ballot_number:04d}.jpg",
    )
    return instance, candidates


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
    assert "scan_result_cache_enabled" in payload["features"]


def test_scan_warm_api_requires_auth_and_warms_runtime(app_module, client):
    _iid, slug = app_module.create_instance("Warm API Test", 5, ["A", "B"])
    instance, candidates = app_module.get_instance(slug)
    app_module.generate_instance_files(instance, candidates)

    unauth = client.post(f"/api/{slug}/scan/warm")
    assert unauth.status_code == 400

    login(client)
    page = client.get(f"/{slug}/")
    token = extract_csrf(page.get_data(as_text=True))

    auth = client.post(
        f"/api/{slug}/scan/warm",
        headers={"X-CSRF-Token": token},
    )
    assert auth.status_code == 200

    payload = auth.get_json()
    assert payload["slug"] == slug
    assert payload["mode"] in {"inprocess", "subprocess"}
    if payload["mode"] == "inprocess":
        assert payload["warmed"] is True
        assert "cache_hit" in payload


def test_get_db_retries_transient_open_errors(app_module, monkeypatch):
    real_connect = app_module.sqlite3.connect
    attempts = {"count": 0}
    slept = []

    def flaky_connect(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise app_module.sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(app_module.sqlite3, "connect", flaky_connect)
    monkeypatch.setattr(app_module, "SQLITE_OPEN_RETRY_COUNT", 5)
    monkeypatch.setattr(app_module, "SQLITE_OPEN_RETRY_DELAY_MS", 1)
    monkeypatch.setattr(app_module, "sleep", lambda seconds: slept.append(seconds))

    with app_module.get_db() as conn:
        row = conn.execute("SELECT 1 AS n").fetchone()

    assert row["n"] == 1
    assert attempts["count"] >= 3
    assert len(slept) >= 2


def test_get_db_rejects_directory_db_path(app_module, tmp_path, monkeypatch):
    directory_path = tmp_path / "db-as-directory"
    directory_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_module, "DB", directory_path)

    with pytest.raises(RuntimeError, match="points to a directory"):
        app_module.get_db()


def test_async_scan_job_create_and_status(app_module, client, monkeypatch):
    _iid, slug = app_module.create_instance("Async Queue Test", 5, ["A", "B"])
    instance, candidates = app_module.get_instance(slug)
    app_module.generate_instance_files(instance, candidates)

    login(client)
    page = client.get(f"/{slug}/")
    token = extract_csrf(page.get_data(as_text=True))

    monkeypatch.setattr(app_module, "ensure_scan_workers_running", lambda: True)

    created = client.post(
        f"/api/{slug}/scan/jobs",
        headers={"X-CSRF-Token": token},
        data={
            "capture_mode": "upload_file",
            "operator_override": "0",
            "ballot": (io.BytesIO(b"job-ballot"), "ballot.png"),
        },
        content_type="multipart/form-data",
    )
    assert created.status_code == 202
    payload = created.get_json()
    assert payload["status"] == "queued"
    job_id = payload["job_id"]

    status = client.get(f"/api/{slug}/scan/jobs/{job_id}")
    assert status.status_code == 200
    status_payload = status.get_json()
    assert status_payload["status"] == "queued"
    assert "queue_position" in status_payload
    assert "active_workers" in status_payload


def test_async_scan_job_backpressure_returns_429(app_module, client, monkeypatch):
    _iid, slug = app_module.create_instance("Async Queue Pressure", 5, ["A", "B"])
    instance, candidates = app_module.get_instance(slug)
    app_module.generate_instance_files(instance, candidates)

    monkeypatch.setattr(app_module, "SCAN_MAX_QUEUED_JOBS", 1)
    with app_module.get_db() as conn:
        conn.execute(
            "INSERT INTO scan_jobs (id, instance_id, slug, status, request_json) "
            "VALUES (?,?,?,?,?)",
            ("queuedjob001", int(instance["id"]), slug, "queued", json.dumps({}, ensure_ascii=False)),
        )
        conn.commit()

    login(client)
    page = client.get(f"/{slug}/")
    token = extract_csrf(page.get_data(as_text=True))

    created = client.post(
        f"/api/{slug}/scan/jobs",
        headers={"X-CSRF-Token": token},
        data={
            "capture_mode": "upload_file",
            "operator_override": "0",
            "ballot": (io.BytesIO(b"queue-pressure"), "ballot.png"),
        },
        content_type="multipart/form-data",
    )
    assert created.status_code == 429
    payload = created.get_json()
    assert payload["error"] == "queue_overloaded"


def test_async_scan_job_dedup_short_circuit_returns_existing_result(app_module, client, monkeypatch):
    _iid, slug = app_module.create_instance("Async Dedup", 5, ["A", "B"])
    instance, candidates = app_module.get_instance(slug)
    app_module.generate_instance_files(instance, candidates)

    monkeypatch.setattr(app_module, "ensure_scan_workers_running", lambda: True)

    login(client)
    page = client.get(f"/{slug}/")
    token = extract_csrf(page.get_data(as_text=True))

    first = client.post(
        f"/api/{slug}/scan/jobs",
        headers={"X-CSRF-Token": token},
        data={
            "capture_mode": "upload_file",
            "operator_override": "0",
            "ballot": (io.BytesIO(b"same-image-bytes"), "ballot.png"),
        },
        content_type="multipart/form-data",
    )
    assert first.status_code == 202
    first_payload = first.get_json()
    first_job_id = first_payload["job_id"]

    with app_module.get_db() as conn:
        row = conn.execute(
            "SELECT input_sha256 FROM scan_jobs WHERE id=?",
            (first_job_id,),
        ).fetchone()
        conn.execute(
            "UPDATE scan_jobs SET status='done', result_html=?, started_at=CURRENT_TIMESTAMP, "
            "finished_at=CURRENT_TIMESTAMP WHERE id=?",
            ("<html>ok</html>", first_job_id),
        )
        conn.commit()
        assert row["input_sha256"]

    second = client.post(
        f"/api/{slug}/scan/jobs",
        headers={"X-CSRF-Token": token},
        data={
            "capture_mode": "upload_file",
            "operator_override": "0",
            "ballot": (io.BytesIO(b"same-image-bytes"), "ballot.png"),
        },
        content_type="multipart/form-data",
    )
    assert second.status_code == 200
    second_payload = second.get_json()
    assert second_payload["dedup_hit"] is True
    assert second_payload["job_id"] == first_job_id
    assert second_payload["status"] == "done"
    assert f"/{slug}/scan/jobs/{first_job_id}/result" in second_payload["result_url"]


def test_async_blank_confirm_endpoint_saves_ballot(app_module, client):
    _iid, slug = app_module.create_instance("Async Blank Confirm", 5, ["Ana", "Mihai"])
    instance, candidates = app_module.get_instance(slug)
    app_module.generate_instance_files(instance, candidates)

    votes = {c["field_id"]: "BLANK" for c in candidates}
    pending = {
        "slug": slug,
        "instance_id": int(instance["id"]),
        "ballot_number": 1,
        "votes": votes,
        "image_path": f"instances/{slug}/scans/ballot_0001.jpg",
        "capture_mode": "manual_live",
        "capture_confidence": 82,
        "operator_override": False,
        "rescan_mode": False,
        "omr_duration_ms": 12,
    }
    with app_module.get_db() as conn:
        conn.execute(
            "INSERT INTO scan_jobs (id, instance_id, slug, status, request_json, result_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                "jobblank0001",
                int(instance["id"]),
                slug,
                "blank_pending",
                json.dumps({}, ensure_ascii=False),
                json.dumps(
                    {
                        "kind": "blank_pending",
                        "ballot_number": 1,
                        "ballot_label": f"V{int(instance['id']):03d}-0001",
                        "pending_blank": pending,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()

    login(client)
    page = client.get(f"/{slug}/")
    token = extract_csrf(page.get_data(as_text=True))

    confirm = client.post(
        f"/api/{slug}/scan/jobs/jobblank0001/confirm-blank",
        headers={"X-CSRF-Token": token},
    )
    assert confirm.status_code == 200
    payload = confirm.get_json()
    assert payload["status"] == "done"
    assert f"/{slug}/scan/jobs/jobblank0001/result" in payload["result_url"]

    with app_module.get_db() as conn:
        ballot = conn.execute(
            "SELECT review_status, needs_rescan FROM scanned_ballots "
            "WHERE instance_id=? AND ballot_number=1",
            (instance["id"],),
        ).fetchone()
        job = conn.execute(
            "SELECT status, result_html FROM scan_jobs WHERE id='jobblank0001'"
        ).fetchone()

    assert ballot is not None
    assert ballot["review_status"] == "pending"
    assert ballot["needs_rescan"] == 1
    assert job["status"] == "done"
    assert "Buletin V" in (job["result_html"] or "")


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


def test_review_page_requires_auth(app_module, client):
    _iid, slug = app_module.create_instance("Review Auth", 5, ["A", "B"])

    response = client.get(f"/{slug}/review", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_review_confirm_advances_queue_and_reopen_resets_status(app_module, client):
    _iid, slug = app_module.create_instance("Review Queue", 10, ["Ana", "Mihai"])
    seed_scanned_ballot(app_module, slug, 1, ["DA", "NU"])
    seed_scanned_ballot(app_module, slug, 2, ["NU", "DA"])

    login(client)
    page = client.get(f"/{slug}/review")
    token = extract_csrf(page.get_data(as_text=True))

    confirm = client.post(
        f"/{slug}/review/1/confirm",
        data={
            "_csrf_token": token,
            "clear_rescan": "1",
        },
        follow_redirects=False,
    )
    assert confirm.status_code == 302
    assert confirm.headers["Location"].endswith(f"/{slug}/review?ballot=2")

    with app_module.get_db() as conn:
        row = conn.execute(
            "SELECT review_status, reviewed_by FROM scanned_ballots "
            "WHERE instance_id=(SELECT id FROM vote_instances WHERE slug=?) AND ballot_number=1",
            (slug,),
        ).fetchone()
    assert row["review_status"] == "confirmed"
    assert row["reviewed_by"] == "admin"

    reopen = client.post(
        f"/{slug}/review/1/reopen",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert reopen.status_code == 302
    assert reopen.headers["Location"].endswith(f"/{slug}/review?ballot=1")

    with app_module.get_db() as conn:
        row = conn.execute(
            "SELECT review_status, reviewed_at, reviewed_by FROM scanned_ballots "
            "WHERE instance_id=(SELECT id FROM vote_instances WHERE slug=?) AND ballot_number=1",
            (slug,),
        ).fetchone()
    assert row["review_status"] == "pending"
    assert row["reviewed_at"] is None
    assert row["reviewed_by"] is None


def test_review_correction_updates_votes_and_results(app_module, client):
    _iid, slug = app_module.create_instance("Review Correct", 5, ["Ana", "Mihai"])
    instance, candidates = seed_scanned_ballot(app_module, slug, 1, ["DA", "NU"])

    login(client)
    page = client.get(f"/{slug}/review?ballot=1")
    token = extract_csrf(page.get_data(as_text=True))

    first_cid = candidates[0]["id"]
    second_cid = candidates[1]["id"]

    response = client.post(
        f"/{slug}/review/1/correct",
        data={
            "_csrf_token": token,
            f"vote_{first_cid}": "NU",
            f"vote_{second_cid}": "NU",
            "reason": "Corectie manuala dupa verificare vizuala.",
            "clear_rescan": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/{slug}/review?ballot=1")

    with app_module.get_db() as conn:
        votes = conn.execute(
            "SELECT c.position, bv.vote "
            "FROM ballot_votes bv "
            "JOIN scanned_ballots sb ON sb.id=bv.scanned_ballot_id "
            "JOIN candidates c ON c.id=bv.candidate_id "
            "WHERE sb.instance_id=? AND sb.ballot_number=1 "
            "ORDER BY c.position",
            (instance["id"],),
        ).fetchall()
        ballot = conn.execute(
            "SELECT review_status, reviewed_by FROM scanned_ballots "
            "WHERE instance_id=? AND ballot_number=1",
            (instance["id"],),
        ).fetchone()
        edits = conn.execute(
            "SELECT old_vote, new_vote, reason, actor "
            "FROM ballot_vote_edits "
            "WHERE instance_id=? AND ballot_number=1",
            (instance["id"],),
        ).fetchall()

    assert [row["vote"] for row in votes] == ["NU", "NU"]
    assert ballot["review_status"] == "corrected"
    assert ballot["reviewed_by"] == "admin"
    assert len(edits) == 1
    assert edits[0]["old_vote"] == "DA"
    assert edits[0]["new_vote"] == "NU"
    assert edits[0]["actor"] == "admin"
    assert "Corectie manuala" in edits[0]["reason"]

    names, data, total_scanned = app_module.get_instance_results(instance)
    assert total_scanned == 1
    assert names == ["Ana", "Mihai"]
    assert data["Ana"]["DA"] == 0
    assert data["Ana"]["NU"] == 1


def test_scan_performance_api_requires_auth_and_returns_payload(app_module, client):
    _iid, slug = app_module.create_instance("Perf API Test", 5, ["A", "B"])

    unauth = client.get(f"/api/{slug}/scan/performance")
    assert unauth.status_code == 401

    login(client)
    auth = client.get(f"/api/{slug}/scan/performance?hours=6")
    assert auth.status_code == 200
    payload = auth.get_json()
    assert payload["instance"]["slug"] == slug
    assert payload["performance"]["lookback_hours"] == 6
    assert isinstance(payload["performance"]["stages"], list)


def test_scan_rescan_reuses_cached_qr_and_omr(app_module, client, monkeypatch):
    _iid, slug = app_module.create_instance("Cache Test", 5, ["Ana", "Mihai"])
    instance, candidates = app_module.get_instance(slug)
    app_module.generate_instance_files(instance, candidates)

    calls = {"qr": 0, "omr": 0}

    monkeypatch.setattr(
        app_module,
        "analyze_image_quality",
        lambda _path: {"status": "ok", "reasons": [], "metrics": {}},
    )

    def fake_qr(_path):
        calls["qr"] += 1
        return (instance["id"], 1)

    def fake_omr(_path, _instance_dir, candidates_map):
        calls["omr"] += 1
        return ({fid: "DA" for fid, _ in candidates_map}, None, None)

    monkeypatch.setattr(app_module, "read_ballot_qr", fake_qr)
    monkeypatch.setattr(app_module, "run_omr_on_path", fake_omr)

    login(client)
    page = client.get(f"/{slug}/")
    token = extract_csrf(page.get_data(as_text=True))

    first = client.post(
        f"/{slug}/scan",
        data={
            "_csrf_token": token,
            "capture_mode": "manual",
            "operator_override": "1",
            "ballot": (io.BytesIO(b"cache-hit-ballot"), "ballot.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert first.status_code == 200

    second = client.post(
        f"/{slug}/scan",
        data={
            "_csrf_token": token,
            "capture_mode": "manual",
            "operator_override": "1",
            "rescan_ballot_number": "1",
            "ballot": (io.BytesIO(b"cache-hit-ballot"), "ballot.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert second.status_code == 200

    assert calls["qr"] == 1
    assert calls["omr"] == 1

    perf = client.get(f"/api/{slug}/scan/performance")
    assert perf.status_code == 200
    payload = perf.get_json()
    omr_stage = next((row for row in payload["performance"]["stages"] if row["stage"] == "omr"), None)
    assert omr_stage is not None
    assert omr_stage["cache_hits"] >= 1

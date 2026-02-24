"""
Multi-Instance Voting App (Upgraded)
===================================
Run:
    source venv/bin/activate
    python3 app.py

Pages:
    Dashboard: http://localhost:5102/
    New:       http://localhost:5102/new
    Scan:      http://localhost:5102/<slug>/
    Results:   http://localhost:5102/<slug>/results
    Ballots:   http://localhost:5102/<slug>/ballots
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import secrets
import socket
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from functools import wraps
from pathlib import Path
from time import monotonic, time

from flask import (
    after_this_request,
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-in-prod")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload limit
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

DB = Path(os.environ.get("VOTES_DB_PATH", "votes.db"))
DATA_DIR = Path(os.environ.get("VOTES_DATA_DIR", "data"))

DEFAULT_ADMIN_USER = os.environ.get("APP_ADMIN_USER", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("APP_ADMIN_PASSWORD", "admin1234")
SESSION_TIMEOUT_SECONDS = int(os.environ.get("APP_SESSION_TIMEOUT_SECONDS", "1800"))
ASSET_VERSION = os.environ.get("APP_ASSET_VERSION", str(int(time())))

ALLOWED_UPLOAD_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

try:
    BALLOT_ZIP_BATCH_LIMIT = max(20, int(os.environ.get("APP_BALLOT_ZIP_BATCH_LIMIT", "100")))
except ValueError:
    BALLOT_ZIP_BATCH_LIMIT = 100

# ── Ballot geometry constants (must match create_ballot.py) ──────────────────
PAGE_W = 500
BUBBLE_W = 45
BUBBLE_H = 45
DA_ORIGIN_X = 280
FIRST_ORIGIN_Y = 135
BUBBLES_GAP = 160
LABELS_GAP = 100
MARKER_SIZE = 44
MARKER_MARGIN = 10
SHEET_TO_MARKER_RATIO = 11

ERROR_MESSAGES = {
    "UPLOAD_MISSING": "Selectati o imagine a buletinului.",
    "UPLOAD_INVALID_FORMAT": "Format imagine neacceptat. Folositi JPG, PNG sau WEBP.",
    "IMAGE_QUALITY_FAIL": "Calitatea imaginii este prea slaba pentru scanare.",
    "QR_NOT_FOUND": "Nu s-a putut citi codul QR al buletinului.",
    "WRONG_INSTANCE": "Buletinul apartine altei alegeri.",
    "INVALID_BALLOT_NUMBER": "Numar buletin invalid pentru aceasta alegere.",
    "DUPLICATE_BALLOT": "Buletin deja scanat. Vot duplicat respins.",
    "OMR_TIMEOUT": "Procesarea OMR a depasit timpul maxim.",
    "OMR_MARKERS_MISSING": "Nu s-au gasit markerii de aliniere ai buletinului.",
    "OMR_MULTIMARK": "Buletin invalid: au fost detectate mai multe bule pe acelasi rand.",
    "OMR_PROCESS_FAILED": "OMRChecker nu a putut procesa imaginea.",
    "LOW_CONFIDENCE_REQUIRES_OVERRIDE": (
        "Scorul capturii este sub pragul minim. "
        "Confirmati override-ul operatorului inainte de trimitere."
    ),
    "RESCAN_TARGET_MISMATCH": (
        "Rescanare invalida: codul QR nu corespunde buletinului selectat pentru rescanare."
    ),
    "RESCAN_TARGET_MISSING": "Buletinul selectat pentru rescanare nu exista in baza de date.",
    "UNEXPECTED_ERROR": "A aparut o eroare neasteptata in timpul scanarii.",
}

QUALITY_REASON_MESSAGES = {
    "very_blurry": "imagine foarte blurata",
    "blurry": "imagine usor blurata",
    "too_dark": "imagine prea intunecata",
    "too_bright": "imagine supraexpusa",
    "frame_missing": "buletinul nu este complet in cadru",
}

PASS_THRESHOLD_BASIS_LABELS = {
    "valid_votes": "DA+NU per candidat",
    "scanned_ballots": "toate buletinele scanate",
    "handed_out_ballots": "buletine distribuite",
    "total_ballots": "buletine totale configurate",
}


# ── Common helpers ─────────────────────────────────────────────────────────────

SCAN_DEBUG_ENABLED = os.environ.get("APP_SCAN_DEBUG", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}


def scan_debug_log(event: str, **fields):
    """Structured scan diagnostics for App Service logs."""
    if not SCAN_DEBUG_ENABLED:
        return
    payload = {"event": event, **fields}
    line = "[scan-debug] " + json.dumps(payload, ensure_ascii=False, default=str)
    try:
        app.logger.warning(line)
    except Exception:
        pass
    # App Service log stream reliably captures stdout/stderr.
    print(line, flush=True)


def page_height(n: int) -> int:
    return 350 + n * 100


def get_db():
    if DB.parent != Path("."):
        DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def wants_json() -> bool:
    return request.path.startswith("/api/")


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_common_context():
    return {
        "csrf_token": csrf_token,
        "is_authenticated": bool(session.get("admin_user")),
        "admin_user": session.get("admin_user"),
        "max_upload_mb": app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
        "asset_version": ASSET_VERSION,
    }


@app.before_request
def enforce_session_and_csrf():
    admin_user = session.get("admin_user")
    last_seen = session.get("last_seen")
    now_ts = int(time())

    if admin_user and last_seen and now_ts - int(last_seen) > SESSION_TIMEOUT_SECONDS:
        session.clear()
        if wants_json():
            return jsonify({"error": "session_expired"}), 401
        flash("Sesiunea a expirat. Autentificati-va din nou.", "error")
        return redirect(url_for("login_page"))

    if admin_user:
        session["last_seen"] = now_ts
        g.admin_user = admin_user
    else:
        g.admin_user = None

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.endpoint in {"static"}:
            return None
        sent = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token")
        expected = session.get("_csrf_token")
        if not sent or not expected or sent != expected:
            if wants_json():
                return jsonify({"error": "invalid_csrf"}), 400
            flash("Token CSRF invalid. Reincarcati pagina si incercati din nou.", "error")
            return redirect(request.referrer or url_for("dashboard"))
    return None


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_user"):
            if wants_json():
                return jsonify({"error": "auth_required"}), 401
            session["next_url"] = request.full_path if request.query_string else request.path
            flash("Autentificare necesara pentru aceasta actiune.", "error")
            return redirect(url_for("login_page"))
        return fn(*args, **kwargs)

    return wrapper


def log_audit(action: str, instance_id=None, ballot_number=None, metadata=None, actor=None):
    actor = actor or session.get("admin_user") or "system"
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_events (actor, action, instance_id, ballot_number, metadata_json) "
            "VALUES (?,?,?,?,?)",
            (actor, action, instance_id, ballot_number, metadata_json),
        )
        conn.commit()


def record_scan_attempt(
    instance_id,
    ballot_number,
    stage,
    status,
    error_code=None,
    duration_ms=None,
):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scan_attempts (instance_id, ballot_number, stage, status, error_code, duration_ms) "
            "VALUES (?,?,?,?,?,?)",
            (instance_id, ballot_number, stage, status, error_code, duration_ms),
        )
        conn.commit()


def friendly_quality_reasons(reasons: list[str]) -> str:
    if not reasons:
        return ""
    translated = [QUALITY_REASON_MESSAGES.get(reason, reason) for reason in reasons]
    return ", ".join(translated)


def resolve_https_ssl_context():
    """
    Resolve SSL context for local HTTPS:
    1) Use FLASK_SSL_CERT + FLASK_SSL_KEY when provided.
    2) Try Werkzeug adhoc if cryptography is installed.
    3) Fallback to a locally generated OpenSSL self-signed cert/key pair.
    """
    cert_env = os.environ.get("FLASK_SSL_CERT")
    key_env = os.environ.get("FLASK_SSL_KEY")

    if cert_env or key_env:
        if not cert_env or not key_env:
            raise RuntimeError(
                "Set both FLASK_SSL_CERT and FLASK_SSL_KEY, or neither."
            )
        cert_path = Path(cert_env).expanduser()
        key_path = Path(key_env).expanduser()
        if not cert_path.exists() or not key_path.exists():
            raise RuntimeError(
                "FLASK_SSL_CERT/FLASK_SSL_KEY files not found."
            )
        return str(cert_path), str(key_path)

    # Werkzueg 'adhoc' path (requires cryptography)
    try:
        import cryptography  # noqa: F401

        return "adhoc"
    except Exception:
        pass

    # Fallback: generate and reuse a local self-signed cert with OpenSSL.
    cert_dir = DATA_DIR / "ssl"
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "dev-cert.pem"
    key_path = cert_dir / "dev-key.pem"

    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    if shutil.which("openssl") is None:
        raise RuntimeError(
            "HTTPS requires one of: cryptography package, OpenSSL binary, "
            "or FLASK_SSL_CERT/FLASK_SSL_KEY."
        )

    # Best-effort LAN IP for SAN; fallback localhost only.
    lan_ip = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        lan_ip = probe.getsockname()[0]
        probe.close()
    except Exception:
        lan_ip = None

    san_entries = ["DNS:localhost", "IP:127.0.0.1"]
    if lan_ip:
        san_entries.append(f"IP:{lan_ip}")

    cmd = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-sha256",
        "-days",
        "365",
        "-keyout",
        str(key_path),
        "-out",
        str(cert_path),
        "-subj",
        "/CN=localhost",
        "-addext",
        f"subjectAltName={','.join(san_entries)}",
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            "OpenSSL certificate generation failed for HTTPS. "
            f"Details: {stderr[:300]}"
        ) from exc

    return str(cert_path), str(key_path)


# ── Database ──────────────────────────────────────────────────────────────────


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with get_db() as conn:
        # Migrate old single-election schema if present
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scanned_ballots'"
        ).fetchone()
        if existing:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(scanned_ballots)").fetchall()]
            if "instance_id" not in cols:
                conn.execute("ALTER TABLE scanned_ballots RENAME TO legacy_scanned_ballots")
                conn.execute("ALTER TABLE ballot_votes    RENAME TO legacy_ballot_votes")
                conn.commit()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vote_instances (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                slug          TEXT    NOT NULL UNIQUE,
                title         TEXT    NOT NULL,
                total_ballots INTEGER NOT NULL DEFAULT 100,
                status        TEXT    NOT NULL DEFAULT 'active',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL REFERENCES vote_instances(id),
                position    INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                field_id    TEXT    NOT NULL,
                UNIQUE (instance_id, position)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scanned_ballots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id   INTEGER NOT NULL REFERENCES vote_instances(id),
                ballot_number INTEGER NOT NULL,
                image_path    TEXT,
                needs_rescan  INTEGER NOT NULL DEFAULT 0,
                rescan_note   TEXT,
                ts            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (instance_id, ballot_number)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ballot_votes (
                scanned_ballot_id INTEGER NOT NULL REFERENCES scanned_ballots(id),
                candidate_id      INTEGER NOT NULL REFERENCES candidates(id),
                vote              TEXT    NOT NULL CHECK(vote IN ('DA','NU','BLANK')),
                PRIMARY KEY (scanned_ballot_id, candidate_id)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login_at TIMESTAMP
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                actor        TEXT NOT NULL,
                action       TEXT NOT NULL,
                instance_id  INTEGER,
                ballot_number INTEGER,
                metadata_json TEXT,
                ts           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_attempts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id   INTEGER NOT NULL,
                ballot_number INTEGER,
                stage         TEXT NOT NULL,
                status        TEXT NOT NULL,
                error_code    TEXT,
                duration_ms   INTEGER,
                ts            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS instance_analytics_settings (
                instance_id             INTEGER PRIMARY KEY REFERENCES vote_instances(id) ON DELETE CASCADE,
                handed_out_ballots      INTEGER,
                manual_null_ballots     INTEGER NOT NULL DEFAULT 0,
                pass_threshold_enabled  INTEGER NOT NULL DEFAULT 0 CHECK(pass_threshold_enabled IN (0, 1)),
                pass_threshold_pct      REAL NOT NULL DEFAULT 60,
                pass_threshold_basis    TEXT NOT NULL DEFAULT 'valid_votes',
                updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        # Forward-compatible schema updates for existing databases.
        sb_cols = [row[1] for row in conn.execute("PRAGMA table_info(scanned_ballots)").fetchall()]
        if "needs_rescan" not in sb_cols:
            conn.execute(
                "ALTER TABLE scanned_ballots "
                "ADD COLUMN needs_rescan INTEGER NOT NULL DEFAULT 0"
            )
        if "rescan_note" not in sb_cols:
            conn.execute("ALTER TABLE scanned_ballots ADD COLUMN rescan_note TEXT")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scanned_ballots_instance_ballot "
            "ON scanned_ballots(instance_id, ballot_number)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scanned_ballots_rescan "
            "ON scanned_ballots(instance_id, needs_rescan, ballot_number)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ballot_votes_candidate ON ballot_votes(candidate_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_instance_ts ON audit_events(instance_id, ts)"
        )

        existing_admin = conn.execute("SELECT id FROM admin_users LIMIT 1").fetchone()
        if not existing_admin:
            conn.execute(
                "INSERT INTO admin_users (username, password_hash) VALUES (?, ?)",
                (DEFAULT_ADMIN_USER, generate_password_hash(DEFAULT_ADMIN_PASSWORD)),
            )

        conn.commit()


# ── Slug / field-id helpers ───────────────────────────────────────────────────


def make_slug(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "alegere"


def to_field_id(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s or "candidat"


# ── DB helpers ────────────────────────────────────────────────────────────────


def get_all_instances():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT vi.*, "
            "  (SELECT COUNT(*) FROM scanned_ballots sb WHERE sb.instance_id=vi.id) AS scanned "
            "FROM vote_instances vi ORDER BY vi.created_at DESC"
        ).fetchall()
    return rows


def get_instance(slug: str):
    """Returns (instance_row, candidates_list) or (None, None)."""
    with get_db() as conn:
        instance = conn.execute("SELECT * FROM vote_instances WHERE slug=?", (slug,)).fetchone()
        if not instance:
            return None, None
        candidates = conn.execute(
            "SELECT * FROM candidates WHERE instance_id=? ORDER BY position",
            (instance["id"],),
        ).fetchall()
    return instance, candidates


def get_scanned_ballot(instance_id: int, ballot_number: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM scanned_ballots WHERE instance_id=? AND ballot_number=?",
            (instance_id, ballot_number),
        ).fetchone()


def get_instance_mis_scans(instance_id: int):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                sb.id,
                sb.ballot_number,
                sb.image_path,
                sb.ts,
                sb.needs_rescan,
                sb.rescan_note,
                SUM(CASE WHEN bv.vote='DA' THEN 1 ELSE 0 END)    AS da_votes,
                SUM(CASE WHEN bv.vote='NU' THEN 1 ELSE 0 END)    AS nu_votes,
                SUM(CASE WHEN bv.vote='BLANK' THEN 1 ELSE 0 END) AS blank_votes
            FROM scanned_ballots sb
            LEFT JOIN ballot_votes bv ON bv.scanned_ballot_id = sb.id
            WHERE sb.instance_id=?
            GROUP BY sb.id, sb.ballot_number, sb.image_path, sb.ts, sb.needs_rescan, sb.rescan_note
            ORDER BY sb.ballot_number
            """,
            (instance_id,),
        ).fetchall()

    flagged = []
    potential = []
    for row in rows:
        item = {
            "id": row["id"],
            "ballot_number": int(row["ballot_number"]),
            "image_path": row["image_path"],
            "ts": row["ts"],
            "needs_rescan": bool(row["needs_rescan"]),
            "rescan_note": row["rescan_note"],
            "da_votes": int(row["da_votes"] or 0),
            "nu_votes": int(row["nu_votes"] or 0),
            "blank_votes": int(row["blank_votes"] or 0),
        }
        if item["needs_rescan"]:
            flagged.append(item)
        elif item["blank_votes"] > 0:
            potential.append(item)
    return flagged, potential


def get_instance_results(instance):
    """Returns (candidate_names_list, data_dict, total_scanned)."""
    iid = instance["id"]
    analytics_settings = get_instance_analytics_settings(iid)
    threshold_enabled = bool(analytics_settings.get("pass_threshold_enabled"))
    threshold_pct = float(analytics_settings.get("pass_threshold_pct", 60.0))
    with get_db() as conn:
        total_scanned = conn.execute(
            "SELECT COUNT(*) FROM scanned_ballots WHERE instance_id=?", (iid,)
        ).fetchone()[0]

        cands = conn.execute(
            "SELECT * FROM candidates WHERE instance_id=? ORDER BY position", (iid,)
        ).fetchall()

        rows = conn.execute(
            "SELECT bv.candidate_id, bv.vote, COUNT(*) AS cnt "
            "FROM ballot_votes bv "
            "JOIN scanned_ballots sb ON sb.id = bv.scanned_ballot_id "
            "WHERE sb.instance_id=? "
            "GROUP BY bv.candidate_id, bv.vote",
            (iid,),
        ).fetchall()

    id_to_name = {c["id"]: c["name"] for c in cands}
    names = [c["name"] for c in cands]
    data = {name: {"DA": 0, "NU": 0, "BLANK": 0} for name in names}

    for row in rows:
        cname = id_to_name.get(row["candidate_id"])
        if cname:
            data[cname][row["vote"]] = row["cnt"]

    for name in names:
        da = data[name]["DA"]
        nu = data[name]["NU"]
        blank = data[name]["BLANK"]
        total_yes_no = da + nu
        total_all = total_yes_no + blank
        data[name]["total"] = total_yes_no
        data[name]["total_all"] = total_all
        data[name]["pct_da"] = round(da / total_yes_no * 100) if total_yes_no else 0
        data[name]["required_yes"] = None

        if threshold_enabled:
            denominator = _pass_denominator(
                analytics_settings,
                instance,
                total_scanned,
                total_yes_no,
            )
            if denominator is not None and denominator > 0:
                required_yes = int(math.ceil(denominator * threshold_pct / 100.0))
                data[name]["required_yes"] = required_yes
                data[name]["elected"] = da >= required_yes
            else:
                data[name]["elected"] = False
        else:
            data[name]["elected"] = da > nu and total_yes_no > 0

    return names, data, total_scanned


def default_instance_analytics_settings():
    return {
        "handed_out_ballots": None,
        "manual_null_ballots": 0,
        "pass_threshold_enabled": False,
        "pass_threshold_pct": 60.0,
        "pass_threshold_basis": "valid_votes",
    }


def get_instance_analytics_settings(instance_id: int):
    settings = default_instance_analytics_settings()
    with get_db() as conn:
        row = conn.execute(
            "SELECT handed_out_ballots, manual_null_ballots, "
            "pass_threshold_enabled, pass_threshold_pct, pass_threshold_basis "
            "FROM instance_analytics_settings WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
    if not row:
        return settings

    basis = row["pass_threshold_basis"] or settings["pass_threshold_basis"]
    if basis not in PASS_THRESHOLD_BASIS_LABELS:
        basis = settings["pass_threshold_basis"]

    settings["handed_out_ballots"] = row["handed_out_ballots"]
    settings["manual_null_ballots"] = max(int(row["manual_null_ballots"] or 0), 0)
    settings["pass_threshold_enabled"] = bool(row["pass_threshold_enabled"])
    settings["pass_threshold_pct"] = float(row["pass_threshold_pct"] or settings["pass_threshold_pct"])
    settings["pass_threshold_pct"] = max(0.0, min(100.0, settings["pass_threshold_pct"]))
    settings["pass_threshold_basis"] = basis
    return settings


def save_instance_analytics_settings(instance_id: int, settings: dict):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO instance_analytics_settings "
            "(instance_id, handed_out_ballots, manual_null_ballots, "
            " pass_threshold_enabled, pass_threshold_pct, pass_threshold_basis, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(instance_id) DO UPDATE SET "
            "handed_out_ballots=excluded.handed_out_ballots, "
            "manual_null_ballots=excluded.manual_null_ballots, "
            "pass_threshold_enabled=excluded.pass_threshold_enabled, "
            "pass_threshold_pct=excluded.pass_threshold_pct, "
            "pass_threshold_basis=excluded.pass_threshold_basis, "
            "updated_at=CURRENT_TIMESTAMP",
            (
                instance_id,
                settings["handed_out_ballots"],
                settings["manual_null_ballots"],
                1 if settings["pass_threshold_enabled"] else 0,
                settings["pass_threshold_pct"],
                settings["pass_threshold_basis"],
            ),
        )
        conn.commit()


def _safe_pct(part: float, whole: float):
    if whole <= 0:
        return None
    return round(part / whole * 100, 2)


def _pass_denominator(settings: dict, instance: dict, total_scanned: int, total_valid: int):
    basis = settings["pass_threshold_basis"]
    if basis == "valid_votes":
        return total_valid
    if basis == "scanned_ballots":
        return total_scanned
    if basis == "handed_out_ballots":
        handed_out = settings.get("handed_out_ballots")
        return int(handed_out) if handed_out is not None else None
    if basis == "total_ballots":
        return int(instance["total_ballots"])
    return total_valid


def build_instance_analytics(instance, candidates):
    iid = instance["id"]
    settings = get_instance_analytics_settings(iid)

    with get_db() as conn:
        total_scanned = conn.execute(
            "SELECT COUNT(*) FROM scanned_ballots WHERE instance_id=?",
            (iid,),
        ).fetchone()[0]

        scanned_blank = conn.execute(
            "SELECT COUNT(*) "
            "FROM scanned_ballots sb "
            "WHERE sb.instance_id=? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM ballot_votes bv "
            "  WHERE bv.scanned_ballot_id=sb.id AND bv.vote IN ('DA','NU')"
            ")",
            (iid,),
        ).fetchone()[0]

        rows = conn.execute(
            "SELECT bv.candidate_id, bv.vote, COUNT(*) AS cnt "
            "FROM ballot_votes bv "
            "JOIN scanned_ballots sb ON sb.id = bv.scanned_ballot_id "
            "WHERE sb.instance_id=? "
            "GROUP BY bv.candidate_id, bv.vote",
            (iid,),
        ).fetchall()

    counts_by_candidate = {
        c["id"]: {"DA": 0, "NU": 0, "BLANK": 0}
        for c in candidates
    }
    for row in rows:
        cid = row["candidate_id"]
        if cid in counts_by_candidate:
            counts_by_candidate[cid][row["vote"]] = int(row["cnt"])

    manual_null = int(settings["manual_null_ballots"])
    total_null = scanned_blank + manual_null
    total_ballots = int(instance["total_ballots"])
    handed_out = settings["handed_out_ballots"]

    if handed_out is None:
        pending_return = None
    else:
        pending_return = max(int(handed_out) - total_scanned - manual_null, 0)

    candidate_rows = []
    for c in candidates:
        counts = counts_by_candidate[c["id"]]
        da = counts["DA"]
        nu = counts["NU"]
        blank = counts["BLANK"]
        total_valid = da + nu
        total_all = total_valid + blank
        margin = da - nu

        required_yes = None
        denominator = None
        status = "pending"
        passed = False

        if settings["pass_threshold_enabled"]:
            denominator = _pass_denominator(settings, instance, total_scanned, total_valid)
            if denominator is not None and denominator > 0:
                required_yes = int(math.ceil(denominator * settings["pass_threshold_pct"] / 100.0))
                passed = da >= required_yes
                status = "pass" if passed else "fail"
        else:
            if total_valid == 0:
                status = "pending"
            else:
                passed = da > nu
                status = "pass" if passed else "fail"

        candidate_rows.append(
            {
                "position": c["position"],
                "name": c["name"],
                "da": da,
                "nu": nu,
                "blank": blank,
                "total_valid": total_valid,
                "total_all": total_all,
                "pct_da_valid": _safe_pct(da, total_valid),
                "pct_participation": _safe_pct(total_valid, total_scanned),
                "margin": margin,
                "required_yes": required_yes,
                "rule_denominator": denominator,
                "passed": passed,
                "status": status,
            }
        )

    with_votes = [row for row in candidate_rows if row["total_valid"] > 0]
    leader = max(with_votes, key=lambda row: row["pct_da_valid"]) if with_votes else None
    tight_race = min(with_votes, key=lambda row: abs(row["margin"])) if with_votes else None

    passed_count = sum(1 for row in candidate_rows if row["status"] == "pass")
    failed_count = sum(1 for row in candidate_rows if row["status"] == "fail")

    return {
        "settings": settings,
        "pass_basis_labels": PASS_THRESHOLD_BASIS_LABELS,
        "overview": {
            "total_ballots": total_ballots,
            "handed_out_ballots": handed_out,
            "total_scanned": total_scanned,
            "scanned_blank_ballots": scanned_blank,
            "manual_null_ballots": manual_null,
            "total_null_ballots": total_null,
            "valid_scanned_ballots": max(total_scanned - scanned_blank, 0),
            "pending_return_ballots": pending_return,
            "turnout_pct_total": _safe_pct(total_scanned, total_ballots),
            "turnout_pct_handed_out": _safe_pct(total_scanned, handed_out or 0),
            "null_rate_processed": _safe_pct(total_null, total_scanned + manual_null),
            "passed_count": passed_count,
            "failed_count": failed_count,
        },
        "pass_rule": {
            "enabled": settings["pass_threshold_enabled"],
            "threshold_pct": settings["pass_threshold_pct"],
            "basis": settings["pass_threshold_basis"],
            "basis_label": PASS_THRESHOLD_BASIS_LABELS[settings["pass_threshold_basis"]],
        },
        "candidates": candidate_rows,
        "insights": {
            "leader": leader,
            "tight_race": tight_race,
        },
    }


def create_instance(title: str, total_ballots: int, names: list[str]):
    """Insert instance + candidates. Returns (instance_id, slug)."""
    slug = make_slug(title)
    with get_db() as conn:
        base_slug = slug
        suffix = 1
        while conn.execute("SELECT 1 FROM vote_instances WHERE slug=?", (slug,)).fetchone():
            slug = f"{base_slug}-{suffix}"
            suffix += 1

        conn.execute(
            "INSERT INTO vote_instances (slug, title, total_ballots) VALUES (?,?,?)",
            (slug, title, total_ballots),
        )
        instance_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        seen_ids = {}
        for i, name in enumerate(names, 1):
            fid = to_field_id(name)
            if fid in seen_ids:
                seen_ids[fid] += 1
                fid = f"{fid}_{seen_ids[fid]}"
            else:
                seen_ids[fid] = 0
            conn.execute(
                "INSERT INTO candidates (instance_id, position, name, field_id) "
                "VALUES (?,?,?,?)",
                (instance_id, i, name, fid),
            )
        conn.commit()

    return instance_id, slug


def generate_instance_files(instance, candidates):
    """Write template.json, config.json, omr_marker.jpg for an instance."""
    slug = instance["slug"]
    instance_dir = DATA_DIR / "instances" / slug
    instance_dir.mkdir(parents=True, exist_ok=True)

    n = len(candidates)
    ph = page_height(n)

    field_ids = [c["field_id"] for c in candidates]

    template = {
        "pageDimensions": [PAGE_W, ph],
        "bubbleDimensions": [BUBBLE_W, BUBBLE_H],
        "emptyValue": "BLANK",
        "preProcessors": [
            {
                "name": "CropOnMarkers",
                "options": {
                    "relativePath": "omr_marker.jpg",
                    "sheetToMarkerWidthRatio": SHEET_TO_MARKER_RATIO,
                },
            }
        ],
        "fieldBlocks": {
            "Voturi": {
                "origin": [DA_ORIGIN_X, FIRST_ORIGIN_Y],
                "fieldLabels": field_ids,
                "bubbleValues": ["DA", "NU"],
                "direction": "horizontal",
                "bubblesGap": BUBBLES_GAP,
                "labelsGap": LABELS_GAP,
            }
        },
    }

    processing_h = round(600 * ph / PAGE_W)
    config = {
        "dimensions": {
            "display_height": ph * 2,
            "display_width": PAGE_W * 2,
            "processing_height": processing_h,
            "processing_width": 600,
        },
        "outputs": {"show_image_level": 0, "save_detections": True},
    }

    with open(instance_dir / "template.json", "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)

    with open(instance_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    from create_ballot import save_marker_file

    save_marker_file(instance_dir / "omr_marker.jpg")


# ── QR code reader ────────────────────────────────────────────────────────────


def _parse_qr_data(data: str):
    """Parse 'V001-0042' (including noisy variants) -> (1, 42) or None."""
    if not data:
        return None
    text = str(data).strip()
    m = re.search(r"V\s*([0-9]{1,4})\s*-\s*([0-9]{1,6})", text, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))

    idx = text.rfind("-")
    if idx > 0:
        left, right = text[:idx], text[idx + 1 :]
        if left.startswith("V") and right.isdigit():
            return int(left[1:]), int(right)
    return None


def read_ballot_qr(image_path: Path):
    """
    Decode a V001-0042 QR code from the ballot image.
    Returns (instance_id, ballot_number) or None.

    Uses multiple OpenCV decode passes (scales + threshold variants + QR zone ROI)
    for better reliability on mobile photos.
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps

        decode_attempts = 0

        pil_img = Image.open(str(image_path))
        pil_img = ImageOps.exif_transpose(pil_img)
        pil_img = pil_img.convert("RGB")
        base_img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        detector = cv2.QRCodeDetector()

        def decode_candidates(img):
            nonlocal decode_attempts
            decode_attempts += 1
            if img is None or getattr(img, "size", 0) == 0:
                return None

            data, _, _ = detector.detectAndDecode(img)
            parsed = _parse_qr_data(data)
            if parsed:
                return parsed

            try:
                ok, decoded_info, _, _ = detector.detectAndDecodeMulti(img)
            except Exception:
                ok, decoded_info = False, []
            if ok and decoded_info:
                for candidate in decoded_info:
                    parsed = _parse_qr_data(candidate)
                    if parsed:
                        return parsed
            return None

        def build_gray_variants(gray):
            variants = []
            variants.append(gray)
            variants.append(cv2.equalizeHist(gray))
            variants.append(cv2.GaussianBlur(gray, (3, 3), 0))

            clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
            variants.append(clahe.apply(gray))

            sharpen = cv2.addWeighted(
                gray,
                1.5,
                cv2.GaussianBlur(gray, (0, 0), 2.0),
                -0.5,
                0,
            )
            variants.append(sharpen)

            _, th_otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            variants.append(th_otsu)
            _, th_inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            variants.append(th_inv)
            variants.append(
                cv2.adaptiveThreshold(
                    gray,
                    255,
                    cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                    cv2.THRESH_BINARY,
                    31,
                    5,
                )
            )
            return variants

        def try_decode_with_variants(img):
            parsed = decode_candidates(img)
            if parsed:
                return parsed

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            for variant in build_gray_variants(gray):
                parsed = decode_candidates(variant)
                if parsed:
                    return parsed

            h, w = gray.shape[:2]
            x0 = int(w * 0.12)
            x1 = int(w * 0.88)
            y0 = int(h * 0.58)
            y1 = int(h * 0.99)
            if x1 - x0 > 100 and y1 - y0 > 100:
                roi_color = img[y0:y1, x0:x1]
                roi_gray = gray[y0:y1, x0:x1]
                for candidate in [roi_color, roi_gray]:
                    parsed = decode_candidates(candidate)
                    if parsed:
                        return parsed

                roi_scaled = cv2.resize(
                    roi_gray,
                    None,
                    fx=1.35,
                    fy=1.35,
                    interpolation=cv2.INTER_CUBIC,
                )
                for variant in build_gray_variants(roi_scaled):
                    parsed = decode_candidates(variant)
                    if parsed:
                        return parsed
            return None

        variants = []
        seen_shapes = set()
        h0, w0 = base_img.shape[:2]
        max_side = max(w0, h0)

        def add_variant(img):
            shape_key = img.shape[:2]
            if shape_key in seen_shapes:
                return
            seen_shapes.add(shape_key)
            variants.append(img)

        add_variant(base_img)
        for target_max_side in (2600, 2200, 1800, 1400):
            scale = target_max_side / max_side
            if abs(scale - 1.0) < 0.08:
                continue
            new_w = max(1, int(round(w0 * scale)))
            new_h = max(1, int(round(h0 * scale)))
            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
            resized = cv2.resize(base_img, (new_w, new_h), interpolation=interpolation)
            add_variant(resized)

        for candidate_img in variants:
            parsed = try_decode_with_variants(candidate_img)
            if parsed:
                scan_debug_log(
                    "qr_decode_success",
                    image_name=image_path.name,
                    parsed_instance_id=parsed[0],
                    parsed_ballot_number=parsed[1],
                    attempts=decode_attempts,
                    variants=len(variants),
                    image_width=w0,
                    image_height=h0,
                )
                return parsed

        scan_debug_log(
            "qr_decode_failed",
            image_name=image_path.name,
            attempts=decode_attempts,
            variants=len(variants),
            image_width=w0,
            image_height=h0,
        )

    except Exception as exc:
        scan_debug_log(
            "qr_decode_exception",
            image_name=image_path.name if image_path else None,
            error=str(exc),
        )
    return None


def analyze_image_quality(image_path: Path):
    """
    Lightweight quality check used by guided capture UX.
    Returns status in {'ok', 'warn', 'fail'} + reasons and metrics.
    """
    try:
        import cv2
        import numpy as np

        buf = np.frombuffer(image_path.read_bytes(), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            return {"status": "fail", "reasons": ["frame_missing"], "metrics": {}}

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        total_area = max(h * w, 1)

        blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(gray.mean())

        edges = cv2.Canny(gray, 70, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        largest_area = max((cv2.contourArea(c) for c in contours), default=0.0)
        frame_ratio = float(largest_area / total_area)

        reasons = []
        status = "ok"

        if blur_score < 12:
            reasons.append("very_blurry")
            status = "fail"
        elif blur_score < 45:
            reasons.append("blurry")
            status = "warn"

        if brightness < 55:
            reasons.append("too_dark")
            status = "warn" if status == "ok" else status
        elif brightness > 225:
            reasons.append("too_bright")
            status = "warn" if status == "ok" else status

        if frame_ratio < 0.04:
            reasons.append("frame_missing")
            status = "fail"
        elif frame_ratio < 0.09 and status == "ok":
            reasons.append("frame_missing")
            status = "warn"

        return {
            "status": status,
            "reasons": reasons,
            "metrics": {
                "blur_score": round(blur_score, 2),
                "brightness": round(brightness, 2),
                "frame_ratio": round(frame_ratio, 4),
            },
        }
    except Exception:
        return {"status": "warn", "reasons": [], "metrics": {}}


def detect_live_marker_alignment(image_path: Path, marker_path: Path):
    """
    Detect corner alignment markers for live camera guidance.
    Returns marker confidence and per-corner status for TL/TR/BL/BR.
    """
    try:
        import cv2
        import numpy as np

        img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        marker = cv2.imread(str(marker_path), cv2.IMREAD_GRAYSCALE)
        if img is None or marker is None:
            return {
                "aligned": False,
                "all_found": False,
                "geometry_ok": False,
                "threshold": 0.44,
                "scores": {},
                "found": {},
                "positions": {},
            }

        h, w = img.shape
        if max(h, w) > 960:
            scale = 960.0 / max(h, w)
            img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            h, w = img.shape

        img = cv2.GaussianBlur(img, (3, 3), 0)
        marker = cv2.GaussianBlur(marker, (3, 3), 0)

        expected_size = max(int(w / SHEET_TO_MARKER_RATIO), 16)
        size_candidates = sorted(
            {
                max(14, int(expected_size * f))
                for f in (0.55, 0.7, 0.85, 1.0, 1.2, 1.4)
            }
        )

        half_h = h // 2
        half_w = w // 2
        regions = {
            "tl": (0, half_h, 0, half_w),
            "tr": (0, half_h, half_w, w),
            "bl": (half_h, h, 0, half_w),
            "br": (half_h, h, half_w, w),
        }

        scores = {}
        found = {}
        positions = {}
        threshold = 0.44

        for key, (y0, y1, x0, x1) in regions.items():
            roi = img[y0:y1, x0:x1]
            rh, rw = roi.shape
            best_score = -1.0
            best_pos = None

            for marker_size in size_candidates:
                if marker_size >= min(rh, rw) - 2:
                    continue
                candidate = cv2.resize(
                    marker,
                    (marker_size, marker_size),
                    interpolation=cv2.INTER_AREA,
                )
                res = cv2.matchTemplate(roi, candidate, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val > best_score:
                    best_score = float(max_val)
                    cx = x0 + max_loc[0] + marker_size / 2
                    cy = y0 + max_loc[1] + marker_size / 2
                    best_pos = (cx, cy)

            if best_score < 0:
                best_score = 0.0
            scores[key] = round(best_score, 4)
            is_found = best_score >= threshold
            found[key] = is_found
            if best_pos:
                positions[key] = {"x": round(float(best_pos[0]), 1), "y": round(float(best_pos[1]), 1)}
            else:
                positions[key] = None

        all_found = all(found.values())
        geometry_ok = False
        if all_found:
            tl = positions["tl"]
            tr = positions["tr"]
            bl = positions["bl"]
            br = positions["br"]

            left_x = (tl["x"] + bl["x"]) / 2
            right_x = (tr["x"] + br["x"]) / 2
            top_y = (tl["y"] + tr["y"]) / 2
            bottom_y = (bl["y"] + br["y"]) / 2

            width_ok = right_x - left_x > w * 0.38
            height_ok = bottom_y - top_y > h * 0.38
            left_order_ok = tl["x"] < tr["x"] and bl["x"] < br["x"]
            top_order_ok = tl["y"] < bl["y"] and tr["y"] < br["y"]
            geometry_ok = bool(width_ok and height_ok and left_order_ok and top_order_ok)

        return {
            "aligned": bool(all_found and geometry_ok),
            "all_found": bool(all_found),
            "geometry_ok": bool(geometry_ok),
            "threshold": threshold,
            "scores": scores,
            "found": found,
            "positions": positions,
            "image_size": {"width": w, "height": h},
        }
    except Exception:
        return {
            "aligned": False,
            "all_found": False,
            "geometry_ok": False,
            "threshold": 0.44,
            "scores": {},
            "found": {},
            "positions": {},
        }


# ── OMR scanning helper ───────────────────────────────────────────────────────


def run_omr_on_path(image_path: Path, instance_dir: Path, candidates_map: list):
    """
    Run OMRChecker on an already-saved image file.
    Returns (votes_dict, error_code, error_message).
    """
    with tempfile.TemporaryDirectory() as tmp:
        inp = Path(tmp) / "inp"
        out = Path(tmp) / "out"
        inp.mkdir()

        shutil.copy(instance_dir / "template.json", inp)
        shutil.copy(instance_dir / "config.json", inp)
        shutil.copy(instance_dir / "omr_marker.jpg", inp)

        dst = inp / ("ballot" + image_path.suffix)
        shutil.copy(image_path, dst)

        proc = subprocess.run(
            [sys.executable, "main.py", "-i", str(inp), "-o", str(out)],
            capture_output=True,
            text=True,
            env={**os.environ, "OMR_HEADLESS": "1"},
            timeout=90,
        )

        for csv_path in sorted((out / "Results").glob("*.csv")):
            with open(csv_path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if not rows:
                continue
            row = rows[0]
            if any(row.get(fid, "").strip() for fid, _ in candidates_map):
                votes = {fid: row.get(fid, "BLANK").strip().upper() for fid, _ in candidates_map}
                return votes, None, None

        multi_csv = out / "Manual" / "MultiMarkedFiles.csv"
        if multi_csv.exists():
            with open(multi_csv, newline="", encoding="utf-8") as f:
                if len(list(csv.DictReader(f))) > 0:
                    return None, "OMR_MULTIMARK", ERROR_MESSAGES["OMR_MULTIMARK"]

        errors_csv = out / "Manual" / "ErrorFiles.csv"
        if errors_csv.exists():
            with open(errors_csv, newline="", encoding="utf-8") as f:
                if len(list(csv.DictReader(f))) > 0:
                    return (
                        None,
                        "OMR_MARKERS_MISSING",
                        ERROR_MESSAGES["OMR_MARKERS_MISSING"],
                    )

        stderr = proc.stderr[-600:] if proc.stderr else ""
        message = ERROR_MESSAGES["OMR_PROCESS_FAILED"]
        if stderr:
            message = f"{message} ({stderr})"
        return None, "OMR_PROCESS_FAILED", message


# ── Ballot ZIP generation ─────────────────────────────────────────────────────


def generate_ballots_zip_file(instance, candidates, zip_path: Path, start_number: int, end_number: int):
    """Generate ballot PNGs in [start_number, end_number] and write them to a ZIP on disk."""
    from create_ballot import make_ballot

    qr_prefix = f"V{instance['id']:03d}"
    candidate_names = [c["name"] for c in candidates]

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        with tempfile.TemporaryDirectory() as tmpdir:
            for n in range(start_number, end_number + 1):
                out_path = str(Path(tmpdir) / f"ballot_{n:04d}.png")
                make_ballot(
                    out_path,
                    number=n,
                    candidates=candidate_names,
                    qr_prefix=qr_prefix,
                    save_preview=False,
                )
                zf.write(out_path, f"ballot_{n:04d}.png")


# ── DB save helper ────────────────────────────────────────────────────────────


def _save_ballot_to_db(instance, candidates, ballot_number, votes, image_path, allow_replace=False):
    """
    Save a ballot and votes. If allow_replace=True and ballot exists, replace votes in-place.
    Returns (scanned_ballot_id, replaced_existing: bool).
    """
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM scanned_ballots WHERE instance_id=? AND ballot_number=?",
            (instance["id"], ballot_number),
        ).fetchone()

        replaced = False
        if existing:
            if not allow_replace:
                raise ValueError("duplicate_ballot")
            sb_id = existing["id"]
            replaced = True
            conn.execute(
                "UPDATE scanned_ballots "
                "SET image_path=?, ts=CURRENT_TIMESTAMP, needs_rescan=0, rescan_note=NULL "
                "WHERE id=?",
                (image_path, sb_id),
            )
            conn.execute("DELETE FROM ballot_votes WHERE scanned_ballot_id=?", (sb_id,))
        else:
            conn.execute(
                "INSERT INTO scanned_ballots (instance_id, ballot_number, image_path, needs_rescan, rescan_note) "
                "VALUES (?,?,?,?,?)",
                (instance["id"], ballot_number, image_path, 0, None),
            )
            sb_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        for c in candidates:
            v = votes.get(c["field_id"], "BLANK")
            conn.execute(
                "INSERT INTO ballot_votes (scanned_ballot_id, candidate_id, vote) "
                "VALUES (?,?,?)",
                (sb_id, c["id"], v),
            )
        conn.commit()
        return sb_id, replaced


# ── Auth routes ───────────────────────────────────────────────────────────────


@app.route("/login", methods=["GET"])
def login_page():
    if session.get("admin_user"):
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_submit():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    with get_db() as conn:
        user = conn.execute("SELECT * FROM admin_users WHERE username=?", (username,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Utilizator sau parola invalida.", "error")
            return redirect(url_for("login_page"))

        conn.execute(
            "UPDATE admin_users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?",
            (user["id"],),
        )
        conn.commit()

    session["admin_user"] = username
    session["last_seen"] = int(time())
    log_audit("login", metadata={"username": username}, actor=username)

    next_url = session.pop("next_url", None)
    # Avoid redirecting to pseudo-pages captured by /<slug>/ (eg /login/, /favicon.ico/)
    if next_url in {"/login", "/login/", "/favicon.ico", "/favicon.ico/"}:
        next_url = None
    return redirect(next_url or url_for("dashboard"))


@app.route("/login/", methods=["GET", "POST"])
def login_with_slash():
    """Accept trailing-slash login URLs and normalize them."""
    return redirect(url_for("login_page"), code=308 if request.method == "POST" else 302)


@app.route("/favicon.ico", methods=["GET"])
@app.route("/favicon.ico/", methods=["GET"])
def favicon():
    """Prevent favicon requests from being interpreted as election slugs."""
    return ("", 204)


@app.route("/logout", methods=["POST"])
@admin_required
def logout_submit():
    username = session.get("admin_user")
    session.clear()
    log_audit("logout", metadata={"username": username}, actor=username or "unknown")
    flash("V-ati delogat.", "info")
    return redirect(url_for("dashboard"))


# ── Web routes ────────────────────────────────────────────────────────────────


@app.route("/")
def dashboard():
    instances = get_all_instances()
    return render_template("index.html", instances=instances)


@app.route("/new", methods=["GET"])
@admin_required
def new_instance_form():
    return render_template("instance_new.html")


@app.route("/new", methods=["POST"])
@admin_required
def new_instance_submit():
    title = request.form.get("title", "").strip()
    total_ballots = request.form.get("total_ballots", "100").strip()
    names = [v.strip() for v in request.form.getlist("candidate_name") if v.strip()]

    if not title:
        flash("Titlul alegerii este obligatoriu.", "error")
        return redirect(url_for("new_instance_form"))
    if not names:
        flash("Adaugati cel putin un candidat.", "error")
        return redirect(url_for("new_instance_form"))
    try:
        total_ballots = int(total_ballots)
        if total_ballots < 1:
            raise ValueError
    except ValueError:
        flash("Numarul de buletine trebuie sa fie un intreg pozitiv.", "error")
        return redirect(url_for("new_instance_form"))

    instance_id, slug = create_instance(title, total_ballots, names)

    instance, candidates = get_instance(slug)
    generate_instance_files(instance, candidates)

    log_audit(
        "instance_created",
        instance_id=instance_id,
        metadata={"slug": slug, "title": title, "candidate_count": len(names)},
    )

    flash(f"Alegerea «{title}» a fost creata.", "info")
    return redirect(url_for("scan_page", slug=slug))


@app.route("/<slug>/")
@admin_required
def scan_page(slug):
    instance, candidates = get_instance(slug)
    if not instance:
        abort(404)

    rescan_target = None
    rescan_raw = (request.args.get("rescan", "") or "").strip()
    if rescan_raw:
        try:
            rescan_ballot_number = int(rescan_raw)
        except ValueError:
            flash("Numarul buletinului pentru rescanare este invalid.", "error")
            return redirect(url_for("scan_page", slug=slug))

        row = get_scanned_ballot(instance["id"], rescan_ballot_number)
        if not row:
            flash(f"{ERROR_MESSAGES['RESCAN_TARGET_MISSING']} (RESCAN_TARGET_MISSING)", "error")
            return redirect(url_for("scan_page", slug=slug))

        rescan_target = {
            "ballot_number": rescan_ballot_number,
            "note": row["rescan_note"],
            "needs_rescan": bool(row["needs_rescan"]),
        }

    candidates_map = [(c["field_id"], c["name"]) for c in candidates]
    return render_template(
        "scan.html",
        instance=instance,
        candidates_map=candidates_map,
        slug=slug,
        rescan_target=rescan_target,
    )


@app.route("/<slug>/scan", methods=["POST"])
@admin_required
def scan_upload(slug):
    instance, candidates = get_instance(slug)
    if not instance:
        abort(404)

    started = monotonic()
    candidates_map = [(c["field_id"], c["name"]) for c in candidates]
    capture_mode = (request.form.get("capture_mode", "unknown") or "unknown").strip()
    operator_override = str(request.form.get("operator_override", "0")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    capture_confidence_raw = request.form.get("capture_confidence", "").strip()
    capture_confidence = None
    if capture_confidence_raw:
        try:
            capture_confidence = max(0.0, min(100.0, float(capture_confidence_raw)))
        except ValueError:
            capture_confidence = None
    rescan_ballot_raw = (request.form.get("rescan_ballot_number", "") or "").strip()
    rescan_ballot_number = None
    if rescan_ballot_raw:
        try:
            rescan_ballot_number = int(rescan_ballot_raw)
        except ValueError:
            flash("Numarul buletinului selectat pentru rescanare este invalid.", "error")
            return redirect(url_for("scan_page", slug=slug))

    scan_debug_log(
        "submit_start",
        slug=slug,
        instance_id=instance["id"],
        capture_mode=capture_mode,
        capture_confidence=capture_confidence,
        operator_override=operator_override,
        rescan_ballot_number=rescan_ballot_number,
        content_type=request.content_type,
        content_length=request.content_length,
        user_agent=(request.headers.get("User-Agent", "") or "")[:180],
    )

    file = request.files.get("ballot")
    if not file or file.filename == "":
        scan_debug_log(
            "reject_upload_missing",
            slug=slug,
            instance_id=instance["id"],
            has_file=bool(file),
            filename=getattr(file, "filename", ""),
        )
        record_scan_attempt(instance["id"], None, "upload", "error", "UPLOAD_MISSING")
        flash(f"{ERROR_MESSAGES['UPLOAD_MISSING']} (UPLOAD_MISSING)", "error")
        return redirect(url_for("scan_page", slug=slug))

    suffix = (Path(file.filename).suffix or ".jpg").lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        scan_debug_log(
            "reject_upload_format",
            slug=slug,
            instance_id=instance["id"],
            filename=file.filename,
            suffix=suffix,
            allowed=sorted(ALLOWED_UPLOAD_SUFFIXES),
        )
        record_scan_attempt(instance["id"], None, "upload", "error", "UPLOAD_INVALID_FORMAT")
        flash(f"{ERROR_MESSAGES['UPLOAD_INVALID_FORMAT']} (UPLOAD_INVALID_FORMAT)", "error")
        return redirect(url_for("scan_page", slug=slug))

    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = Path(tmpdir) / f"ballot{suffix}"
        file.save(img_path)

        quality = analyze_image_quality(img_path)
        scan_debug_log(
            "quality_checked",
            slug=slug,
            instance_id=instance["id"],
            filename=file.filename,
            suffix=suffix,
            size_bytes=img_path.stat().st_size if img_path.exists() else None,
            quality_status=quality.get("status"),
            quality_reasons=quality.get("reasons"),
            quality_metrics=quality.get("metrics"),
        )
        if quality["status"] == "fail":
            reasons = friendly_quality_reasons(quality["reasons"]) or "calitate insuficienta"
            msg = f"{ERROR_MESSAGES['IMAGE_QUALITY_FAIL']} ({reasons})."
            scan_debug_log(
                "reject_quality_fail",
                slug=slug,
                instance_id=instance["id"],
                reasons=quality.get("reasons"),
                metrics=quality.get("metrics"),
            )
            record_scan_attempt(instance["id"], None, "quality", "error", "IMAGE_QUALITY_FAIL")
            flash(f"{msg} (IMAGE_QUALITY_FAIL)", "error")
            return redirect(url_for("scan_page", slug=slug))

        if quality["status"] == "warn":
            reasons = friendly_quality_reasons(quality["reasons"])
            if reasons:
                flash(f"Atentie: {reasons}.", "info")

        result = read_ballot_qr(img_path)
        if result is None:
            scan_debug_log(
                "reject_qr_not_found",
                slug=slug,
                instance_id=instance["id"],
                quality_status=quality.get("status"),
                quality_reasons=quality.get("reasons"),
                quality_metrics=quality.get("metrics"),
            )
            record_scan_attempt(instance["id"], None, "qr_decode", "error", "QR_NOT_FOUND")
            flash(f"{ERROR_MESSAGES['QR_NOT_FOUND']} (QR_NOT_FOUND)", "error")
            return redirect(url_for("scan_page", slug=slug))

        parsed_instance_id, ballot_number = result
        replace_existing = False
        scan_debug_log(
            "qr_parsed",
            slug=slug,
            instance_id=instance["id"],
            parsed_instance_id=parsed_instance_id,
            ballot_number=ballot_number,
            rescan_ballot_number=rescan_ballot_number,
        )

        if capture_confidence is not None and capture_confidence < 65 and not operator_override:
            scan_debug_log(
                "reject_low_confidence",
                slug=slug,
                instance_id=instance["id"],
                ballot_number=ballot_number,
                capture_confidence=capture_confidence,
                operator_override=operator_override,
            )
            record_scan_attempt(
                instance["id"],
                ballot_number,
                "preflight",
                "error",
                "LOW_CONFIDENCE_REQUIRES_OVERRIDE",
            )
            flash(
                f"{ERROR_MESSAGES['LOW_CONFIDENCE_REQUIRES_OVERRIDE']} "
                "(LOW_CONFIDENCE_REQUIRES_OVERRIDE)",
                "error",
            )
            return redirect(url_for("scan_page", slug=slug))

        if parsed_instance_id != instance["id"]:
            scan_debug_log(
                "reject_wrong_instance",
                slug=slug,
                instance_id=instance["id"],
                parsed_instance_id=parsed_instance_id,
                ballot_number=ballot_number,
            )
            record_scan_attempt(
                instance["id"],
                ballot_number,
                "instance_check",
                "error",
                "WRONG_INSTANCE",
            )
            flash(
                f"{ERROR_MESSAGES['WRONG_INSTANCE']} "
                f"(QR: V{parsed_instance_id:03d}, alegere curenta: V{instance['id']:03d}) "
                "(WRONG_INSTANCE)",
                "error",
            )
            return redirect(url_for("scan_page", slug=slug))

        if ballot_number < 1 or ballot_number > instance["total_ballots"]:
            scan_debug_log(
                "reject_ballot_range",
                slug=slug,
                instance_id=instance["id"],
                ballot_number=ballot_number,
                total_ballots=instance["total_ballots"],
            )
            record_scan_attempt(
                instance["id"],
                ballot_number,
                "range_check",
                "error",
                "INVALID_BALLOT_NUMBER",
            )
            flash(
                f"{ERROR_MESSAGES['INVALID_BALLOT_NUMBER']}: {ballot_number} "
                "(INVALID_BALLOT_NUMBER)",
                "error",
            )
            return redirect(url_for("scan_page", slug=slug))

        if rescan_ballot_number is not None and ballot_number != rescan_ballot_number:
            scan_debug_log(
                "reject_rescan_target_mismatch",
                slug=slug,
                instance_id=instance["id"],
                ballot_number=ballot_number,
                rescan_ballot_number=rescan_ballot_number,
            )
            record_scan_attempt(
                instance["id"],
                ballot_number,
                "rescan_target_check",
                "error",
                "RESCAN_TARGET_MISMATCH",
            )
            flash(f"{ERROR_MESSAGES['RESCAN_TARGET_MISMATCH']} (RESCAN_TARGET_MISMATCH)", "error")
            return redirect(url_for("scan_page", slug=slug, rescan=rescan_ballot_number))

        with get_db() as conn:
            dup = conn.execute(
                "SELECT id, ts, needs_rescan FROM scanned_ballots WHERE instance_id=? AND ballot_number=?",
                (instance["id"], ballot_number),
            ).fetchone()
        if dup and rescan_ballot_number is None:
            prefix = f"V{instance['id']:03d}"
            record_scan_attempt(
                instance["id"],
                ballot_number,
                "duplicate_check",
                "error",
                "DUPLICATE_BALLOT",
            )
            scan_debug_log(
                "reject_duplicate",
                slug=slug,
                instance_id=instance["id"],
                ballot_number=ballot_number,
                existing_ts=dup["ts"][:19] if dup and dup["ts"] else None,
            )
            flash(
                f"{ERROR_MESSAGES['DUPLICATE_BALLOT']} "
                f"({prefix}-{ballot_number:04d}, {dup['ts'][:16]}) (DUPLICATE_BALLOT)",
                "error",
            )
            return redirect(url_for("scan_page", slug=slug))
        if rescan_ballot_number is not None and not dup:
            scan_debug_log(
                "reject_rescan_target_missing",
                slug=slug,
                instance_id=instance["id"],
                ballot_number=ballot_number,
                rescan_ballot_number=rescan_ballot_number,
            )
            record_scan_attempt(
                instance["id"],
                ballot_number,
                "rescan_target_check",
                "error",
                "RESCAN_TARGET_MISSING",
            )
            flash(f"{ERROR_MESSAGES['RESCAN_TARGET_MISSING']} (RESCAN_TARGET_MISSING)", "error")
            return redirect(url_for("scan_page", slug=slug))
        if rescan_ballot_number is not None and dup:
            replace_existing = True

        instance_dir = DATA_DIR / "instances" / slug
        scan_debug_log(
            "omr_start",
            slug=slug,
            instance_id=instance["id"],
            ballot_number=ballot_number,
            filename=file.filename,
            replace_existing=replace_existing,
        )
        try:
            votes, err_code, err_message = run_omr_on_path(img_path, instance_dir, candidates_map)
        except subprocess.TimeoutExpired:
            duration_ms = int((monotonic() - started) * 1000)
            scan_debug_log(
                "reject_omr_timeout",
                slug=slug,
                instance_id=instance["id"],
                ballot_number=ballot_number,
                duration_ms=duration_ms,
            )
            record_scan_attempt(
                instance["id"],
                ballot_number,
                "omr",
                "error",
                "OMR_TIMEOUT",
                duration_ms,
            )
            flash(f"{ERROR_MESSAGES['OMR_TIMEOUT']} (OMR_TIMEOUT)", "error")
            return redirect(url_for("scan_page", slug=slug))
        except Exception as exc:
            duration_ms = int((monotonic() - started) * 1000)
            scan_debug_log(
                "reject_omr_exception",
                slug=slug,
                instance_id=instance["id"],
                ballot_number=ballot_number,
                duration_ms=duration_ms,
                error=str(exc),
            )
            record_scan_attempt(
                instance["id"],
                ballot_number,
                "omr",
                "error",
                "UNEXPECTED_ERROR",
                duration_ms,
            )
            flash(f"{ERROR_MESSAGES['UNEXPECTED_ERROR']}: {exc}", "error")
            return redirect(url_for("scan_page", slug=slug))

        if err_code:
            duration_ms = int((monotonic() - started) * 1000)
            scan_debug_log(
                "reject_omr_error",
                slug=slug,
                instance_id=instance["id"],
                ballot_number=ballot_number,
                duration_ms=duration_ms,
                err_code=err_code,
                err_message=err_message,
            )
            record_scan_attempt(
                instance["id"],
                ballot_number,
                "omr",
                "error",
                err_code,
                duration_ms,
            )
            flash(f"{err_message} ({err_code})", "error")
            return redirect(url_for("scan_page", slug=slug))

        scan_dir = DATA_DIR / "instances" / slug / "scans"
        scan_dir.mkdir(parents=True, exist_ok=True)
        dest = scan_dir / f"ballot_{ballot_number:04d}{suffix}"
        shutil.copy(img_path, dest)
        relative_image_path = str(dest.relative_to(DATA_DIR))

    prefix = f"V{instance['id']:03d}"
    ballot_label = f"{prefix}-{ballot_number:04d}"
    blank_count = sum(1 for v in votes.values() if v == "BLANK")

    if all(v == "BLANK" for v in votes.values()):
        session["pending_blank"] = {
            "slug": slug,
            "instance_id": instance["id"],
            "ballot_number": ballot_number,
            "votes": votes,
            "image_path": relative_image_path,
            "capture_mode": capture_mode,
            "capture_confidence": capture_confidence,
            "operator_override": operator_override,
            "rescan_mode": replace_existing,
        }
        record_scan_attempt(
            instance["id"],
            ballot_number,
            "final",
            "warn",
            "ALL_BLANK_PENDING_CONFIRM",
            int((monotonic() - started) * 1000),
        )
        log_audit(
            "scan_blank_pending",
            instance_id=instance["id"],
            ballot_number=ballot_number,
            metadata={
                "quality": quality,
                "capture_mode": capture_mode,
                "capture_confidence": capture_confidence,
                "operator_override": operator_override,
            },
        )
        scan_debug_log(
            "blank_pending_confirm",
            slug=slug,
            instance_id=instance["id"],
            ballot_number=ballot_number,
            capture_mode=capture_mode,
            capture_confidence=capture_confidence,
            operator_override=operator_override,
        )
        return render_template(
            "scan.html",
            instance=instance,
            candidates_map=candidates_map,
            scan_votes=votes,
            ballot_number=ballot_number,
            ballot_label=ballot_label,
            blank_warning=True,
            quality=quality,
            slug=slug,
            rescan_target={"ballot_number": rescan_ballot_number} if rescan_ballot_number else None,
        )

    _save_ballot_to_db(
        instance,
        candidates,
        ballot_number,
        votes,
        relative_image_path,
        allow_replace=replace_existing,
    )
    mis_scan_flagged = False
    if blank_count > 0:
        auto_note = (
            f"Detectate {blank_count} campuri BLANK. "
            "Verificare recomandata; rescaneaza daca buletinul are marcaje lipsa."
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE scanned_ballots "
                "SET needs_rescan=1, rescan_note=COALESCE(NULLIF(rescan_note,''), ?) "
                "WHERE instance_id=? AND ballot_number=?",
                (auto_note, instance["id"], ballot_number),
            )
            conn.commit()
        mis_scan_flagged = True

    duration_ms = int((monotonic() - started) * 1000)
    record_scan_attempt(instance["id"], ballot_number, "final", "ok", None, duration_ms)
    if operator_override:
        log_audit(
            "scan_operator_override",
            instance_id=instance["id"],
            ballot_number=ballot_number,
            metadata={
                "capture_mode": capture_mode,
                "capture_confidence": capture_confidence,
            },
        )
    log_audit(
        "scan_rescanned" if replace_existing else "scan_saved",
        instance_id=instance["id"],
        ballot_number=ballot_number,
        metadata={
            "quality": quality,
            "duration_ms": duration_ms,
            "capture_mode": capture_mode,
            "capture_confidence": capture_confidence,
            "operator_override": operator_override,
            "replace_existing": replace_existing,
            "mis_scan_flagged": mis_scan_flagged,
        },
    )
    vote_summary = {
        "DA": sum(1 for v in votes.values() if v == "DA"),
        "NU": sum(1 for v in votes.values() if v == "NU"),
        "BLANK": sum(1 for v in votes.values() if v == "BLANK"),
    }
    scan_debug_log(
        "scan_saved",
        slug=slug,
        instance_id=instance["id"],
        ballot_number=ballot_number,
        duration_ms=duration_ms,
        capture_mode=capture_mode,
        capture_confidence=capture_confidence,
        operator_override=operator_override,
        replace_existing=replace_existing,
        mis_scan_flagged=mis_scan_flagged,
        vote_summary=vote_summary,
        quality_status=quality.get("status"),
        quality_reasons=quality.get("reasons"),
    )

    return render_template(
        "scan.html",
        instance=instance,
        candidates_map=candidates_map,
        scan_votes=votes,
        ballot_number=ballot_number,
        ballot_label=ballot_label,
        quality=quality,
        slug=slug,
        rescan_applied=replace_existing,
        mis_scan_flagged=mis_scan_flagged,
    )


@app.route("/<slug>/scan/confirm-blank", methods=["POST"])
@admin_required
def confirm_blank(slug):
    instance, candidates = get_instance(slug)
    if not instance:
        abort(404)

    pending = session.pop("pending_blank", None)
    if not pending or pending["slug"] != slug:
        flash("Sesiunea a expirat. Rescaneaza buletinul.", "error")
        return redirect(url_for("scan_page", slug=slug))

    ballot_number = pending["ballot_number"]
    votes = pending["votes"]
    relative_image_path = pending["image_path"]
    capture_mode = pending.get("capture_mode")
    capture_confidence = pending.get("capture_confidence")
    operator_override = bool(pending.get("operator_override"))
    rescan_mode = bool(pending.get("rescan_mode"))

    if not rescan_mode:
        with get_db() as conn:
            dup = conn.execute(
                "SELECT ts FROM scanned_ballots WHERE instance_id=? AND ballot_number=?",
                (instance["id"], ballot_number),
            ).fetchone()
        if dup:
            flash("Buletinul a fost deja inregistrat.", "error")
            return redirect(url_for("scan_page", slug=slug))

    _save_ballot_to_db(
        instance,
        candidates,
        ballot_number,
        votes,
        relative_image_path,
        allow_replace=rescan_mode,
    )
    auto_note = (
        "Buletin BLANK confirmat. Verificare recomandata; rescaneaza daca a fost completat gresit."
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE scanned_ballots "
            "SET needs_rescan=1, rescan_note=COALESCE(NULLIF(rescan_note,''), ?) "
            "WHERE instance_id=? AND ballot_number=?",
            (auto_note, instance["id"], ballot_number),
        )
        conn.commit()

    record_scan_attempt(instance["id"], ballot_number, "blank_confirm", "ok", None)
    if operator_override:
        log_audit(
            "scan_operator_override",
            instance_id=instance["id"],
            ballot_number=ballot_number,
            metadata={
                "capture_mode": capture_mode,
                "capture_confidence": capture_confidence,
            },
        )
    log_audit(
        "scan_blank_rescanned" if rescan_mode else "scan_blank_confirmed",
        instance_id=instance["id"],
        ballot_number=ballot_number,
        metadata={
            "capture_mode": capture_mode,
            "capture_confidence": capture_confidence,
            "operator_override": operator_override,
            "replace_existing": rescan_mode,
            "mis_scan_flagged": True,
        },
    )

    candidates_map = [(c["field_id"], c["name"]) for c in candidates]
    prefix = f"V{instance['id']:03d}"
    ballot_label = f"{prefix}-{ballot_number:04d}"

    return render_template(
        "scan.html",
        instance=instance,
        candidates_map=candidates_map,
        scan_votes=votes,
        ballot_number=ballot_number,
        ballot_label=ballot_label,
        slug=slug,
        rescan_applied=rescan_mode,
        mis_scan_flagged=True,
    )


@app.route("/<slug>/scans/<path:filename>")
def serve_scan(slug, filename):
    """Serve a stored ballot scan image."""
    if "/" in filename or "\\" in filename or ".." in filename:
        abort(404)
    scan_dir = (DATA_DIR / "instances" / slug / "scans").resolve()
    file_path = (scan_dir / filename).resolve()
    if not str(file_path).startswith(str(scan_dir)):
        abort(404)
    return send_file(file_path)


@app.route("/<slug>/analytics")
def analytics_page(slug):
    instance, candidates = get_instance(slug)
    if not instance:
        abort(404)

    analytics = build_instance_analytics(instance, candidates)
    return render_template(
        "analytics.html",
        instance=instance,
        candidates=candidates,
        analytics=analytics,
        slug=slug,
    )


@app.route("/<slug>/analytics/settings", methods=["POST"])
@admin_required
def analytics_settings_submit(slug):
    instance, _ = get_instance(slug)
    if not instance:
        abort(404)

    handed_out_raw = request.form.get("handed_out_ballots", "").strip()
    manual_null_raw = request.form.get("manual_null_ballots", "").strip()
    threshold_raw = request.form.get("pass_threshold_pct", "").strip()
    basis_raw = request.form.get("pass_threshold_basis", "valid_votes").strip()
    enabled = request.form.get("pass_threshold_enabled") in {"1", "on", "true", "yes"}

    handed_out = None
    if handed_out_raw != "":
        try:
            handed_out = int(handed_out_raw)
        except ValueError:
            flash("Buletine distribuite trebuie sa fie numar intreg.", "error")
            return redirect(url_for("analytics_page", slug=slug))
        if handed_out < 0:
            flash("Buletine distribuite nu poate fi negativ.", "error")
            return redirect(url_for("analytics_page", slug=slug))
        if handed_out > int(instance["total_ballots"]):
            flash("Buletine distribuite nu poate depasi totalul configurat.", "error")
            return redirect(url_for("analytics_page", slug=slug))

    try:
        manual_null = int(manual_null_raw or "0")
    except ValueError:
        flash("Buletine nule manual trebuie sa fie numar intreg.", "error")
        return redirect(url_for("analytics_page", slug=slug))
    if manual_null < 0:
        flash("Buletine nule manual nu poate fi negativ.", "error")
        return redirect(url_for("analytics_page", slug=slug))
    if handed_out is not None and manual_null > handed_out:
        flash("Buletine nule manual nu poate depasi buletinele distribuite.", "error")
        return redirect(url_for("analytics_page", slug=slug))

    try:
        threshold = float(threshold_raw or "60")
    except ValueError:
        flash("Pragul procentual trebuie sa fie numeric.", "error")
        return redirect(url_for("analytics_page", slug=slug))
    if threshold < 0 or threshold > 100:
        flash("Pragul procentual trebuie sa fie intre 0 si 100.", "error")
        return redirect(url_for("analytics_page", slug=slug))

    if basis_raw not in PASS_THRESHOLD_BASIS_LABELS:
        flash("Baza pragului selectata este invalida.", "error")
        return redirect(url_for("analytics_page", slug=slug))
    if basis_raw == "handed_out_ballots" and handed_out is None:
        flash("Completati «Buletine distribuite» pentru baza «buletine distribuite».", "error")
        return redirect(url_for("analytics_page", slug=slug))

    settings = {
        "handed_out_ballots": handed_out,
        "manual_null_ballots": manual_null,
        "pass_threshold_enabled": enabled,
        "pass_threshold_pct": threshold,
        "pass_threshold_basis": basis_raw,
    }
    save_instance_analytics_settings(instance["id"], settings)
    log_audit(
        "analytics_settings_updated",
        instance_id=instance["id"],
        metadata=settings,
    )
    flash("Setarile de analitice au fost salvate.", "info")
    return redirect(url_for("analytics_page", slug=slug))


@app.route("/<slug>/results")
def results_page(slug):
    instance, candidates = get_instance(slug)
    if not instance:
        abort(404)

    names, data, total_scanned = get_instance_results(instance)
    analytics_settings = get_instance_analytics_settings(instance["id"])
    pass_rule = {
        "enabled": bool(analytics_settings["pass_threshold_enabled"]),
        "threshold_pct": float(analytics_settings["pass_threshold_pct"]),
        "basis": analytics_settings["pass_threshold_basis"],
        "basis_label": PASS_THRESHOLD_BASIS_LABELS.get(
            analytics_settings["pass_threshold_basis"],
            analytics_settings["pass_threshold_basis"],
        ),
    }

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    per_page = 48
    offset = (page - 1) * per_page

    with get_db() as conn:
        total_gallery = conn.execute(
            "SELECT COUNT(*) FROM scanned_ballots WHERE instance_id=?",
            (instance["id"],),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT ballot_number, image_path, ts "
            "FROM scanned_ballots "
            "WHERE instance_id=? "
            "ORDER BY ballot_number "
            "LIMIT ? OFFSET ?",
            (instance["id"], per_page, offset),
        ).fetchall()

    scanned_ballots = [
        {
            "ballot_number": r["ballot_number"],
            "filename": Path(r["image_path"]).name if r["image_path"] else None,
            "ts": r["ts"],
        }
        for r in rows
    ]

    total_pages = max((total_gallery + per_page - 1) // per_page, 1)
    flagged_mis_scans, potential_mis_scans = get_instance_mis_scans(instance["id"])

    return render_template(
        "results.html",
        instance=instance,
        candidates=names,
        data=data,
        total_scanned=total_scanned,
        scanned_ballots=scanned_ballots,
        slug=slug,
        page=page,
        total_pages=total_pages,
        pass_rule=pass_rule,
        flagged_mis_scans=flagged_mis_scans,
        potential_mis_scans=potential_mis_scans,
    )


@app.route("/<slug>/mis-scans/flag", methods=["POST"])
@admin_required
def flag_mis_scan(slug):
    instance, _ = get_instance(slug)
    if not instance:
        abort(404)

    ballot_raw = (request.form.get("ballot_number", "") or "").strip()
    note = (request.form.get("note", "") or "").strip()
    try:
        ballot_number = int(ballot_raw)
    except ValueError:
        flash("Numarul buletinului este invalid.", "error")
        return redirect(url_for("results_page", slug=slug))

    row = get_scanned_ballot(instance["id"], ballot_number)
    if not row:
        flash("Buletinul selectat nu exista in baza de date.", "error")
        return redirect(url_for("results_page", slug=slug))

    if not note:
        note = "Marcat manual pentru reverificare / rescanare."

    with get_db() as conn:
        conn.execute(
            "UPDATE scanned_ballots SET needs_rescan=1, rescan_note=? WHERE id=?",
            (note[:280], row["id"]),
        )
        conn.commit()

    log_audit(
        "mis_scan_flagged",
        instance_id=instance["id"],
        ballot_number=ballot_number,
        metadata={"note": note[:280]},
    )
    flash(f"Buletinul V{instance['id']:03d}-{ballot_number:04d} a fost marcat pentru rescanare.", "info")
    return redirect(url_for("results_page", slug=slug))


@app.route("/<slug>/mis-scans/clear", methods=["POST"])
@admin_required
def clear_mis_scan(slug):
    instance, _ = get_instance(slug)
    if not instance:
        abort(404)

    ballot_raw = (request.form.get("ballot_number", "") or "").strip()
    try:
        ballot_number = int(ballot_raw)
    except ValueError:
        flash("Numarul buletinului este invalid.", "error")
        return redirect(url_for("results_page", slug=slug))

    row = get_scanned_ballot(instance["id"], ballot_number)
    if not row:
        flash("Buletinul selectat nu exista in baza de date.", "error")
        return redirect(url_for("results_page", slug=slug))

    with get_db() as conn:
        conn.execute(
            "UPDATE scanned_ballots SET needs_rescan=0, rescan_note=NULL WHERE id=?",
            (row["id"],),
        )
        conn.commit()

    log_audit(
        "mis_scan_cleared",
        instance_id=instance["id"],
        ballot_number=ballot_number,
    )
    flash(f"Buletinul V{instance['id']:03d}-{ballot_number:04d} a fost scos din lista de mis-scan.", "info")
    return redirect(url_for("results_page", slug=slug))


@app.route("/<slug>/results/export.csv")
def results_export_csv(slug):
    instance, _ = get_instance(slug)
    if not instance:
        abort(404)

    names, data, total_scanned = get_instance_results(instance)

    content = io.StringIO()
    writer = csv.writer(content)

    writer.writerow(["titlu_alegere", instance["title"]])
    writer.writerow(["slug", instance["slug"]])
    writer.writerow(["prefix_qr", f"V{instance['id']:03d}"])
    writer.writerow(["total_buletine", instance["total_ballots"]])
    writer.writerow(["scanate", total_scanned])
    writer.writerow([])

    writer.writerow(["candidat", "DA", "NU", "BLANK", "total_DA_NU", "procent_DA", "status"])
    for name in names:
        row = data[name]
        writer.writerow(
            [
                name,
                row["DA"],
                row["NU"],
                row["BLANK"],
                row["total"],
                row["pct_da"],
                "ALES" if row["elected"] else "RESPINS",
            ]
        )

    log_audit("results_export_csv", instance_id=instance["id"])

    payload = io.BytesIO(content.getvalue().encode("utf-8"))
    payload.seek(0)
    return send_file(
        payload,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"rezultate_{slug}.csv",
    )


@app.route("/<slug>/analytics/export.csv")
def analytics_export_csv(slug):
    instance, candidates = get_instance(slug)
    if not instance:
        abort(404)

    analytics = build_instance_analytics(instance, candidates)
    overview = analytics["overview"]
    pass_rule = analytics["pass_rule"]

    content = io.StringIO()
    writer = csv.writer(content)

    writer.writerow(["titlu_alegere", instance["title"]])
    writer.writerow(["slug", instance["slug"]])
    writer.writerow(["prefix_qr", f"V{instance['id']:03d}"])
    writer.writerow(["total_buletine", overview["total_ballots"]])
    writer.writerow(["buletine_distribuite", overview["handed_out_ballots"] or ""])
    writer.writerow(["scanate", overview["total_scanned"]])
    writer.writerow(["nule_scanate_blank", overview["scanned_blank_ballots"]])
    writer.writerow(["nule_manual", overview["manual_null_ballots"]])
    writer.writerow(["nule_total", overview["total_null_ballots"]])
    writer.writerow(["rata_participare_total_pct", overview["turnout_pct_total"] or ""])
    writer.writerow(["rata_participare_distribuite_pct", overview["turnout_pct_handed_out"] or ""])
    writer.writerow(["rata_nulitate_pct", overview["null_rate_processed"] or ""])
    writer.writerow(["prag_activ", "DA" if pass_rule["enabled"] else "NU"])
    writer.writerow(["prag_pct", pass_rule["threshold_pct"]])
    writer.writerow(["prag_baza", pass_rule["basis"]])
    writer.writerow([])

    writer.writerow(
        [
            "pozitie",
            "candidat",
            "DA",
            "NU",
            "BLANK",
            "DA_NU_total",
            "procent_DA_valid",
            "marja_DA_minus_NU",
            "prag_numar_DA_necesar",
            "status",
        ]
    )
    for row in analytics["candidates"]:
        writer.writerow(
            [
                row["position"],
                row["name"],
                row["da"],
                row["nu"],
                row["blank"],
                row["total_valid"],
                row["pct_da_valid"] if row["pct_da_valid"] is not None else "",
                row["margin"],
                row["required_yes"] if row["required_yes"] is not None else "",
                "TRECUT" if row["status"] == "pass" else ("RESPINS" if row["status"] == "fail" else "IN ASTEPTARE"),
            ]
        )

    log_audit("analytics_export_csv", instance_id=instance["id"])

    payload = io.BytesIO(content.getvalue().encode("utf-8"))
    payload.seek(0)
    return send_file(
        payload,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"analitice_{slug}.csv",
    )


@app.route("/<slug>/ballots")
@admin_required
def ballots_page(slug):
    instance, candidates = get_instance(slug)
    if not instance:
        abort(404)
    total = int(instance["total_ballots"])
    ballot_chunks = []
    for start in range(1, total + 1, BALLOT_ZIP_BATCH_LIMIT):
        end = min(total, start + BALLOT_ZIP_BATCH_LIMIT - 1)
        ballot_chunks.append(
            {
                "start": start,
                "end": end,
                "count": end - start + 1,
            }
        )
    return render_template(
        "instance_ballots.html",
        instance=instance,
        candidates=candidates,
        slug=slug,
        ballot_zip_batch_limit=BALLOT_ZIP_BATCH_LIMIT,
        ballot_chunks=ballot_chunks,
    )


@app.route("/<slug>/ballots/download")
@admin_required
def download_ballots(slug):
    instance, candidates = get_instance(slug)
    if not instance:
        abort(404)
    total = int(instance["total_ballots"])

    start_raw = (request.args.get("start", "") or "").strip()
    end_raw = (request.args.get("end", "") or "").strip()
    start = 1
    end = total
    if start_raw or end_raw:
        try:
            start = int(start_raw or "1")
            end = int(end_raw or str(total))
        except ValueError:
            flash("Interval invalid. Introduceti valori numerice pentru start/end.", "error")
            return redirect(url_for("ballots_page", slug=slug))

    if start < 1 or end < 1 or start > end or end > total:
        flash(
            f"Interval invalid. Valorile trebuie sa fie intre 1 si {total}, iar start <= end.",
            "error",
        )
        return redirect(url_for("ballots_page", slug=slug))

    count = end - start + 1
    if count > BALLOT_ZIP_BATCH_LIMIT:
        flash(
            f"Setul este prea mare pentru o singura arhiva pe serverul live. "
            f"Descarcati in loturi de maximum {BALLOT_ZIP_BATCH_LIMIT} buletine.",
            "error",
        )
        return redirect(url_for("ballots_page", slug=slug))

    with tempfile.NamedTemporaryFile(prefix=f"ballots_{slug}_", suffix=".zip", delete=False) as tmp_zip:
        zip_path = Path(tmp_zip.name)

    try:
        generate_ballots_zip_file(instance, candidates, zip_path, start, end)
    except Exception as exc:
        scan_debug_log(
            "ballots_download_failed",
            slug=slug,
            instance_id=instance["id"],
            start=start,
            end=end,
            count=count,
            error=str(exc)[:400],
        )
        try:
            zip_path.unlink()
        except Exception:
            pass
        flash("Generarea arhivei a esuat. Reincercati cu un interval mai mic.", "error")
        return redirect(url_for("ballots_page", slug=slug))

    @after_this_request
    def cleanup_temp_zip(response):
        try:
            zip_path.unlink(missing_ok=True)
        except Exception:
            pass
        return response

    filename = (
        f"buletine_{slug}.zip"
        if start == 1 and end == total
        else f"buletine_{slug}_{start:04d}-{end:04d}.zip"
    )
    log_audit(
        "ballots_download",
        instance_id=instance["id"],
        metadata={"start": start, "end": end, "count": count},
    )
    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
        max_age=0,
        conditional=False,
    )


@app.route("/<slug>/reset", methods=["POST"])
@admin_required
def reset_instance(slug):
    instance, _ = get_instance(slug)
    if not instance:
        abort(404)

    confirm = request.form.get("confirm", "")
    if confirm == "STERGE":
        with get_db() as conn:
            conn.execute(
                "DELETE FROM ballot_votes WHERE scanned_ballot_id IN "
                "(SELECT id FROM scanned_ballots WHERE instance_id=?)",
                (instance["id"],),
            )
            conn.execute(
                "DELETE FROM scanned_ballots WHERE instance_id=?",
                (instance["id"],),
            )
            conn.commit()

        scan_dir = DATA_DIR / "instances" / slug / "scans"
        if scan_dir.exists():
            for f in scan_dir.iterdir():
                try:
                    f.unlink()
                except Exception:
                    pass

        log_audit("instance_reset", instance_id=instance["id"])
        flash("Toate voturile si imaginile au fost sterse.", "info")
    else:
        flash("Confirmare incorecta. Voturile nu au fost sterse.", "error")

    return redirect(url_for("results_page", slug=slug))


# ── API routes ────────────────────────────────────────────────────────────────


@app.route("/api/<slug>/scan/health")
@admin_required
def api_scan_health(slug):
    instance, _ = get_instance(slug)
    if not instance:
        return jsonify({"error": "not_found"}), 404

    return jsonify(
        {
            "instance": {
                "id": instance["id"],
                "slug": instance["slug"],
                "title": instance["title"],
                "total_ballots": instance["total_ballots"],
            },
            "limits": {"max_upload_mb": app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)},
            "features": {
                "guided_capture": True,
                "blank_confirmation": True,
                "live_marker_detection": True,
                "client_marker_detection": True,
                "quality_checks": ["blur", "brightness", "frame"],
            },
        }
    )


@app.route("/api/<slug>/scan/validate-image", methods=["POST"])
@admin_required
def api_validate_image(slug):
    instance, _ = get_instance(slug)
    if not instance:
        return jsonify({"error": "not_found"}), 404

    f = request.files.get("ballot")
    if not f or f.filename == "":
        scan_debug_log(
            "api_validate_missing_file",
            slug=slug,
            instance_id=instance["id"],
        )
        return jsonify({"error": "missing_file"}), 400

    suffix = (Path(f.filename).suffix or ".jpg").lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        scan_debug_log(
            "api_validate_invalid_format",
            slug=slug,
            instance_id=instance["id"],
            filename=f.filename,
            suffix=suffix,
        )
        return jsonify({"error": "invalid_format"}), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = Path(tmpdir) / f"preview{suffix}"
        f.save(img_path)
        quality = analyze_image_quality(img_path)
        scan_debug_log(
            "api_validate_result",
            slug=slug,
            instance_id=instance["id"],
            filename=f.filename,
            suffix=suffix,
            size_bytes=img_path.stat().st_size if img_path.exists() else None,
            quality_status=quality.get("status"),
            quality_reasons=quality.get("reasons"),
            quality_metrics=quality.get("metrics"),
        )

    return jsonify(quality)


@app.route("/api/<slug>/scan/live-frame-check", methods=["POST"])
@admin_required
def api_live_frame_check(slug):
    instance, _ = get_instance(slug)
    if not instance:
        return jsonify({"error": "not_found"}), 404

    f = request.files.get("ballot")
    if not f or f.filename == "":
        return jsonify({"error": "missing_file"}), 400

    suffix = (Path(f.filename).suffix or ".jpg").lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        return jsonify({"error": "invalid_format"}), 400

    instance_dir = DATA_DIR / "instances" / slug
    marker_path = instance_dir / "omr_marker.jpg"
    if not marker_path.exists():
        return jsonify({"error": "missing_marker"}), 500

    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = Path(tmpdir) / f"live{suffix}"
        f.save(img_path)
        quality = analyze_image_quality(img_path)
        markers = detect_live_marker_alignment(img_path, marker_path)

    return jsonify(
        {
            "quality": quality,
            "markers": markers,
            "ready_to_capture": bool(markers.get("aligned") and quality.get("status") != "fail"),
        }
    )


@app.route("/api/<slug>/results/summary")
def api_results_summary(slug):
    instance, _ = get_instance(slug)
    if not instance:
        return jsonify({"error": "not_found"}), 404

    names, data, total_scanned = get_instance_results(instance)
    analytics_settings = get_instance_analytics_settings(instance["id"])
    payload = {
        "instance": {
            "id": instance["id"],
            "slug": instance["slug"],
            "title": instance["title"],
            "total_ballots": instance["total_ballots"],
        },
        "pass_rule": {
            "enabled": bool(analytics_settings["pass_threshold_enabled"]),
            "threshold_pct": float(analytics_settings["pass_threshold_pct"]),
            "basis": analytics_settings["pass_threshold_basis"],
            "basis_label": PASS_THRESHOLD_BASIS_LABELS.get(
                analytics_settings["pass_threshold_basis"],
                analytics_settings["pass_threshold_basis"],
            ),
        },
        "total_scanned": total_scanned,
        "candidates": [
            {
                "name": name,
                "da": data[name]["DA"],
                "nu": data[name]["NU"],
                "blank": data[name]["BLANK"],
                "pct_da": data[name]["pct_da"],
                "required_yes": data[name].get("required_yes"),
                "elected": data[name]["elected"],
            }
            for name in names
        ],
        "generated_at": int(time()),
    }
    return jsonify(payload)


@app.route("/api/<slug>/analytics/summary")
def api_analytics_summary(slug):
    instance, candidates = get_instance(slug)
    if not instance:
        return jsonify({"error": "not_found"}), 404

    analytics = build_instance_analytics(instance, candidates)
    payload = {
        "instance": {
            "id": instance["id"],
            "slug": instance["slug"],
            "title": instance["title"],
            "total_ballots": instance["total_ballots"],
        },
        "overview": analytics["overview"],
        "pass_rule": analytics["pass_rule"],
        "candidates": analytics["candidates"],
        "generated_at": int(time()),
    }
    return jsonify(payload)


# ── Ensure DB is always initialised (covers `flask run`, reloader, and direct) ─

init_db()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    use_https = os.environ.get("FLASK_USE_HTTPS", "0").lower() in {"1", "true", "yes", "on"}
    ssl_context = None
    if use_https:
        ssl_context = resolve_https_ssl_context()
    print()
    print("=" * 56)
    print("   VotBiserica — Platforma de Vot pentru Biserici")
    print("=" * 56)
    print("  Dashboard:  http://localhost:5102/")
    print("  Login:      http://localhost:5102/login")
    if use_https:
        print("  HTTPS:      https://localhost:5102/")
        print("  Retea HTTPS:https://<ip-local>:5102/ (certificat self-signed)")
        if isinstance(ssl_context, tuple):
            print(f"  Cert:       {ssl_context[0]}")
            print(f"  Key:        {ssl_context[1]}")
    print(f"  DB:         {DB}")
    print("=" * 56)
    print()
    run_kwargs = {"debug": True, "host": "0.0.0.0", "port": 5102}
    if use_https:
        run_kwargs["ssl_context"] = ssl_context
    app.run(**run_kwargs)

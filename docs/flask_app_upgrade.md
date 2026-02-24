# Flask Voting App Upgrade Notes

## Runtime Defaults

- `FLASK_SECRET_KEY`: session secret (required in production)
- `VOTES_DB_PATH`: SQLite path (default `votes.db`)
- `VOTES_DATA_DIR`: data root (default `data`)
- `APP_ADMIN_USER`: initial admin username (default `admin`)
- `APP_ADMIN_PASSWORD`: initial admin password (default `admin1234`)
- `APP_SESSION_TIMEOUT_SECONDS`: idle session timeout (default `1800`)
- `APP_OMR_EXECUTION_MODE`: OMR engine mode (`inprocess` default, or `subprocess`)
- `APP_OMR_TIMEOUT_SECONDS`: subprocess OMR timeout in seconds (default `90`)
- `APP_OMR_TEMPLATE_CACHE_SIZE`: per-thread in-process template cache size (default `12`)
- `APP_OPENCV_NUM_THREADS`: OpenCV thread cap per worker thread (default `1`)
- `APP_SCAN_RESULT_CACHE`: enable scan result cache (`1` default)
- `APP_SCAN_RESULT_CACHE_TTL_SECONDS`: cache TTL in seconds (default `1200`)
- `APP_SCAN_STAGE_METRICS`: enable stage timing persistence (`1` default)
- `APP_SCAN_STAGE_METRICS_SAMPLE_RATE`: sampling rate for successful stage metrics (`0.0..1.0`, default `1.0`; errors are always recorded)
- `APP_SCAN_ASYNC_ENABLED`: enable async scan job queue (`1` default)
- `APP_SCAN_JOB_POLL_INTERVAL_MS`: worker polling interval in milliseconds (default `350`)
- `APP_SCAN_JOB_RESULT_MAX_AGE_HOURS`: retention window for scan job rows/files (default `24`)
- `APP_SCAN_WORKER_THREADS`: async worker threads per app process (default `2`)
- `APP_SCAN_MAX_QUEUED_JOBS`: queue backpressure threshold (default `120`; `POST /scan/jobs` returns `429` above it)
- `APP_SCAN_JOB_DEDUP_TTL_SECONDS`: duplicate upload short-circuit window (default `1800`)
- `APP_SCAN_STARTUP_WARM_ENABLED`: startup warm thread toggle for active elections (default `1`)
- `APP_SCAN_STARTUP_WARM_MAX_INSTANCES`: max active elections warmed at startup/worker boot (default `25`)
- `APP_SQLITE_WAL`: enable SQLite WAL mode (`1` default)
- `APP_SQLITE_BUSY_TIMEOUT_MS`: SQLite busy timeout in milliseconds (default `8000`)
- `MPLCONFIGDIR`: optional matplotlib cache directory (defaults to `VOTES_DATA_DIR/.mplconfig`)

On first run, if no admin exists, one user is seeded from `APP_ADMIN_USER`/`APP_ADMIN_PASSWORD`.

## Security Changes

- CSRF is required on all mutating requests.
- Admin auth is required for:
  - `/new`
  - `/<slug>/`
  - `/<slug>/scan`
  - `/<slug>/scan/confirm-blank`
  - `/<slug>/ballots`
  - `/<slug>/ballots/download`
  - `/<slug>/review`
  - `/<slug>/review/<ballot_number>/confirm`
  - `/<slug>/review/<ballot_number>/correct`
  - `/<slug>/review/<ballot_number>/reopen`
  - `/<slug>/reset`
  - scan-related API endpoints
- Session timeout enforced by `APP_SESSION_TIMEOUT_SECONDS`.

## New API Endpoints

- `GET /api/<slug>/scan/health`
- `POST /api/<slug>/scan/warm`
- `POST /api/<slug>/scan/jobs`
- `GET /api/<slug>/scan/jobs/<job_id>`
- `POST /api/<slug>/scan/jobs/<job_id>/confirm-blank`
- `GET /api/<slug>/scan/performance?hours=24`
- `POST /api/<slug>/scan/validate-image`
- `GET /api/<slug>/results/summary`

## New Reporting Route

- `GET /<slug>/results/export.csv`

## Performance Notes

- OMR now defaults to `inprocess` mode for faster steady-state scoring on web workers.
- If in-process OMR fails with a generic processing error, the app automatically retries using subprocess mode for robustness.
- Runtime OMR config forces `show_image_level=0`, `save_image_level=0`, `save_detections=false` to reduce disk I/O overhead during scans.
- In-process OMR now reuses per-thread cached `Template` objects keyed by instance runtime-file fingerprints, removing repeated template/config rebuild overhead on each scan.
- Scan page now opportunistically pre-warms OMR runtime (`POST /api/<slug>/scan/warm`) on load to reduce first-scan latency.
- Scan submit now supports async queue mode: browser uploads once, receives a job id, polls status, then redirects to final rendered result when worker completes.
- Async queue now supports multi-worker execution (`APP_SCAN_WORKER_THREADS`), queue backpressure, queue position + ETA in status payloads, and duplicate upload short-circuit by image hash.
- Startup warm now runs automatically for active elections and each worker thread warms templates on boot for faster first jobs.
- OpenCV worker threading is configurable via `APP_OPENCV_NUM_THREADS` (default `1`) to prevent CPU oversubscription on multi-worker web deployments.
- Repeated rescans of the same image now reuse cached QR + OMR output (keyed by `instance_id + image_sha256 + candidate_signature`) to avoid duplicate processing work.
- Per-stage timing is persisted in `scan_stage_metrics` and exposed via `GET /api/<slug>/scan/performance` for hotspot analysis (`avg/p50/p95/max`, cache-hit rate, errors); successful metric writes can be sampled to reduce DB load.
- SQLite now uses configurable busy timeout and optional WAL mode to reduce write contention under concurrent web scans.
- A repo-level `gunicorn.conf.py` now provides default worker/thread/preload settings (`workers=2`, `threads=2`, `preload_app=True`) and can be tuned via `GUNICORN_*` env vars.

## UX Changes

- Shared design system under `static/css/`.
- Mobile-first guided scan page with:
  - Stepper
  - Camera + upload tabs
  - Image quality validation (blur/brightness/frame)
  - Live marker dots for the 4 corners (TL/TR/BL/BR)
  - Client-side adaptive marker detection (no per-frame server calls)
  - Auto-capture when alignment is stable for consecutive frames
  - Auto deskew + crop preview before submit
  - Capture confidence score + operator override workflow
  - Better processing states and warnings
- Results page supports print mode and paginated scan gallery.

## Capture Metadata

The scan form now posts:

- `capture_mode` (`auto_live`, `manual_live`, `upload_file`, etc.)
- `capture_confidence` (`0..100`)
- `operator_override` (`0|1`)

If confidence is below threshold (`<65`) and override is not set, the scan is rejected with `LOW_CONFIDENCE_REQUIRES_OVERRIDE`.
All fields are written into audit metadata (`scan_saved`, `scan_blank_pending`, and override events).

## New Tables

- `admin_users`
- `audit_events`
- `scan_attempts`
- `scan_stage_metrics`
- `scan_result_cache`
- `scan_jobs`

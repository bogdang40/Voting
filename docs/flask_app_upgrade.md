# Flask Voting App Upgrade Notes

## Runtime Defaults

- `FLASK_SECRET_KEY`: session secret (required in production)
- `VOTES_DB_PATH`: SQLite path (default `votes.db`)
- `VOTES_DATA_DIR`: data root (default `data`)
- `APP_ADMIN_USER`: initial admin username (default `admin`)
- `APP_ADMIN_PASSWORD`: initial admin password (default `admin1234`)
- `APP_SESSION_TIMEOUT_SECONDS`: idle session timeout (default `1800`)

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
  - `/<slug>/reset`
  - scan-related API endpoints
- Session timeout enforced by `APP_SESSION_TIMEOUT_SECONDS`.

## New API Endpoints

- `GET /api/<slug>/scan/health`
- `POST /api/<slug>/scan/validate-image`
- `GET /api/<slug>/results/summary`

## New Reporting Route

- `GET /<slug>/results/export.csv`

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

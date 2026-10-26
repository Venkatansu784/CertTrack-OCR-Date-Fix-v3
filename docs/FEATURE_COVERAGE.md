# Feature coverage matrix

This matrix maps the Corporate Compliance and Certification Tracker project abstract, UI design document, and database design document to the current application. It also records the deployment and usability additions in CertTrack Ultimate.

| Requirement / capability | Implementation | Notes |
|---|---|---|
| Role-specific views | `Admin`, `HR_Staff`, `Manager`; route decorators and scoped query helpers | Manager access is read-only and restricted to their department. |
| Employee onboarding and directory | `/employees`, employee create/edit, search, safe delete and CSV export | Unique employee code/email; delete is blocked when historical records or a user account exists. |
| Locations and departments | `/admin/manage` | CRUD with relationship checks. |
| Providers and certification programs | `/admin/manage` | Configurable validity months; programs referenced by history cannot be deleted. |
| Certificate management | `/certificates`, create/edit/save, archive/restore, renew | Renewal creates a linked new instance; archive preserves history. |
| Tesseract OCR | `/certificates/upload`, `/certificates/scan`, `/certificates/review/<scan_id>` | Image/scanned PDF OCR plus embedded PDF text extraction. User reviews fields before save. |
| Date extraction | `extract_certificate_fields()` and `infer_expiry_when_not_printed()` | Label-aware date association; validity-period fallback is marked inferred. OCR is not certificate validation. |
| Compliance status | `status_for()` used by dashboard/register/reminders/reports | Calculated dynamically from expiry dates. |
| Reminders | `ensure_reminders_for_record()`, `/reminders` | 30-, 7- and 1-day milestones; idempotent; due items can be acknowledged. |
| Reminder email | `send_reminders.py` | SMTP and a daily scheduler must be configured externally. |
| Reporting | `/reports`, CSV export endpoints | Department summaries and report details; managers see their scope. |
| Audit | `system_audit_log`, `/audit-log` | Admin-only audit view; application routes do not offer editing or deletion of audit rows. |
| Account administration | `/admin/manage` | Accounts map to employees, passwords are hashed, active flag/role/reset available. |
| Storage safety | Local folder by default; optional private Supabase Storage | Hosted durable file retention requires `SUPABASE_URL`, a service-side key, and a private bucket. |
| Supabase Postgres | `SUPABASE_DB_URL`/`DATABASE_URL` support; PostgreSQL schema initialization | SQLite remains default for local development. Postgres tables enable RLS; the server-side app enforces role checks. |
| Deployment | `Dockerfile`, `render.yaml`, `Procfile`, `/healthz` | Docker image includes Tesseract and runs Gunicorn. |
| User experience additions | Responsive visual redesign, light/dark toggle, mobile nav, focus states | Theme choice persists in the browser. |
| Safety additions | CSRF checks, secure session flags, password hashing, file size/type checks, protected file route | Public hosting still needs environment secrets, HTTPS, backup and operational configuration. |

## UI design mapping

The original UI design's login, dashboard, CRUD forms, dark theme and compliance summary remain implemented in Jinja templates rather than separate static-only HTML files. The refreshed styling lives in `static/css/style.css`; browser interactions live in `static/js/app.js`. Added pages include Administration, Reminder Hub, Reports and Audit Log.

## Deployment notes

- SQLite is used locally unless a PostgreSQL connection string is set.
- Empty Supabase setup requires `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD` (12+ characters); the app then creates the initial administrator and minimal master data.
- Supabase Storage is optional locally, but required for persistent uploaded certificate files when the host filesystem is ephemeral.
- Email delivery requires SMTP and an external scheduler.
- OCR extraction must always be reviewed against the original credential.

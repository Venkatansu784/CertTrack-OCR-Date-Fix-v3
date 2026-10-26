# CertTrack Ultimate

A complete full-stack **Corporate Compliance & Certification Tracker** built with Flask, SQLite for local development, optional Supabase PostgreSQL for hosted data, Tesseract OCR, and optional private Supabase Storage for certificate files.

The application keeps the original project workflow: employee directory, role-based access, certificate upload and OCR review, editable issue/expiry dates, derived compliance status, archive/history, renewals, in-app reminders, CSV reports, user administration, and an audit trail. It also adds a responsive visual refresh, remembered light/dark theme, cloud deployment files, a liveness endpoint, optional SMTP reminder delivery, and cloud file persistence.

## 1. Run locally on Windows

### Requirements

- Python 3.10+ (Python 3.12 is recommended for the Docker deployment parity).
- Tesseract OCR for Windows. This is a separate program from the Python `pytesseract` package.
- VS Code or another terminal is optional.

### Start

1. Extract this whole folder and open **the folder containing `app.py`** in VS Code.
2. Run `setup_windows.bat` once.
3. Confirm Tesseract is detected with `tesseract --version`. The launcher checks common Windows installation paths automatically.
4. Run `run_windows.bat`.
5. Open <http://127.0.0.1:5001> (the Windows launcher uses port 5001 to avoid conflicts with an older CertTrack server).

A fresh local SQLite database is seeded with demonstration records for convenience:

| Role | Username | Password |
|---|---|---|
| Admin | `admin@certtrack.local` | `Admin@123` |
| HR Staff | `hr@certtrack.local` | `HR@123` |
| Manager | `manager@certtrack.local` | `Manager@123` |

These demo accounts only appear on the local SQLite login page. **Never use them in a public deployment.**

## 2. Use Supabase PostgreSQL

The app uses its existing server-side query layer against either local SQLite or PostgreSQL. Set a valid Supabase PostgreSQL connection URL and the app initializes its tables/indexes, enables RLS on the application tables, and creates the first Admin account if the database is empty.

### Create the Supabase project

1. Create a Supabase project and save its database password.
2. In Supabase, open **Connect** and copy a PostgreSQL connection string. For a hosted service, use the **Session pooler** connection if your host is IPv4-only; Supabase documents direct, session-pooler, and transaction-pooler choices in its [PostgreSQL connection guide](https://supabase.com/docs/guides/database/connecting-to-postgres).
3. Replace the placeholder password. If the password contains reserved URL characters (`@`, `#`, `%`, `?`, `/`, etc.), percent-encode them.
4. Set the environment variable `SUPABASE_DB_URL` to that complete `postgresql://...` URL. Do **not** use the Supabase project API URL here.
5. Set `SECRET_KEY` to a long random secret and set `INITIAL_ADMIN_USERNAME` and `INITIAL_ADMIN_PASSWORD` before the first start. The bootstrap password must contain at least 12 characters.

When the Supabase database is empty, the app creates a minimal Main Office / Administration / initial Admin setup plus common provider and certification-program rows. It deliberately does **not** seed sample employees or demo certificate records in hosted mode. After signing in, use Administration to set up your real company.

The application connects with a private PostgreSQL connection string. It does not expose the database password in the browser. The database tables have Row Level Security enabled without public-client policies; application access is mediated by the Flask authentication/role checks. Keep the database URL private.

### Local `.env` example

1. Copy `.env.example` to `.env` in the same folder as `app.py`.
2. Fill in `SUPABASE_DB_URL`, `SECRET_KEY`, `INITIAL_ADMIN_USERNAME`, and `INITIAL_ADMIN_PASSWORD`.
3. Restart the app. `python-dotenv` loads the `.env` file automatically.

Leave `SUPABASE_DB_URL` blank to keep using SQLite locally. The local demo accounts are only seeded when SQLite has no user accounts.

## 3. Persist uploaded certificates with Supabase Storage

A hosted container's filesystem is not durable across every redeploy. To keep original certificate PDFs/images, configure Supabase Storage:

1. In Supabase Storage, create a **private** bucket named `certificates` (or set your own bucket name in `SUPABASE_STORAGE_BUCKET`).
2. Set `SUPABASE_URL` to the project URL, e.g. `https://YOUR_PROJECT_REF.supabase.co`.
3. Set `SUPABASE_SERVICE_ROLE_KEY` to the server-side service-role/secret key. Use it **only as a server environment variable**; never paste it into HTML, JavaScript, a public GitHub repository, or a browser build.
4. Restart/redeploy. New uploads are stored in the private bucket, and certificate downloads are fetched by the Flask server after it validates the signed-in user's access.

If storage is not configured, local mode stores files in `instance/uploads`. Hosted PostgreSQL mode deliberately refuses to save a newly uploaded certificate until private Supabase Storage is configured, preventing an unnoticed upload into an ephemeral container filesystem.

## 4. Deploy the website publicly (GitHub + Render)

This repo includes a `Dockerfile` and `render.yaml`. The Docker build installs Tesseract OCR on the server, installs the Python dependencies, and runs Flask using Gunicorn. The `/healthz` endpoint is provided for the hosting health check.

1. Create a private GitHub repository and push the **contents** of this folder (not a nested folder containing another copy of it). The included GitHub Actions workflow runs the regression tests on each push and pull request.
2. In Render, choose **New → Blueprint** (or create a Docker web service) and connect the repository.
3. Review the settings in `render.yaml`. When prompted for unsynced environment variables, provide:
   - `SUPABASE_DB_URL` — PostgreSQL connection string from Supabase Connect.
   - `INITIAL_ADMIN_USERNAME` — the first administrator's company email/username.
   - `INITIAL_ADMIN_PASSWORD` — unique password of at least 12 characters.
   - `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` — required if certificate files must persist in Supabase Storage.
4. Render generates `SECRET_KEY` from the Blueprint config. Keep `COOKIE_SECURE=1` and `SHOW_DEMO_LOGINS=0` in production.
5. Deploy. Open the generated `onrender.com` address and sign in with the bootstrap Admin username/password.
6. Change the initial password using Administration after the first sign-in, then create employees and other user accounts.

Render's [Flask deployment guide](https://render.com/docs/deploy-flask) and [Docker guide](https://render.com/docs/docker) describe its web-service flow. The Dockerfile is used here so the deployed environment also contains Tesseract. Render free instances may spin down when idle; consider the host plan limits before using it for business-critical compliance deadlines.

### Reminder email delivery

In-app reminder records are created by the application. Email sending is an optional separate worker and is not automatically delivered by viewing the dashboard. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` and `SMTP_USE_TLS`, then schedule `python send_reminders.py` once daily through your host's cron/scheduler or Windows Task Scheduler. When SMTP is not configured, the script sends nothing.

## 5. Feature list

- Admin / HR Staff / read-only department Manager roles.
- Employee create, edit, search, safe delete, and CSV export.
- Location and department management.
- Provider and certification-program management with configurable validity months.
- Certificate scan/upload, OCR-assisted data entry, review step, manually editable values, protected download, duplicate credential checks, archive/restore, edit and linked renewal history.
- Date-label-aware issue/expiry parsing; unlabelled/inferred dates are identified rather than described as read from the certificate.
- Current Valid / Expiring Soon (within 30 days) / Expired status calculation.
- 30-, 7-, and 1-day reminder milestones; late reminders keep original schedule dates and are idempotent.
- Reminder acknowledgement, due filtering, CSV reminder export, and optional SMTP email worker.
- Department compliance summaries, report details, CSV export, audit log, and account management/password reset.
- CSRF checks on POST forms, password hashing, upload size/type limits, safe filename handling, role checks, security headers, and private-storage file downloads.
- Responsive refreshed design, hover/focus states, mobile navigation, and browser-persisted light/dark mode.
- SQLite local mode and Supabase PostgreSQL hosted mode.

## 6. Database and files

- `app.py` — Flask routes, access checks, OCR pipeline, database compatibility layer and migrations.
- `schema.sql` — SQLite schema reference.
- `schema_supabase.sql` — PostgreSQL schema reference; automatic initialization is also performed by the app on first boot.
- `templates/` — Jinja templates for login, dashboard, employees, certificates, OCR review, reports, reminders, administration, audit log and errors.
- `static/css/style.css` — responsive visual system and light/dark themes.
- `static/js/app.js` — browser-side form preview, upload state, theme toggle and responsive navigation.
- `send_reminders.py` — optional SMTP reminder worker.
- `tests/` — regression tests for certificate-date association and reminder milestone idempotency.
- `docs/FEATURE_COVERAGE.md` — requirement-to-feature mapping.

## 7. Test locally

After setup, run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

This exercises the labelled-date regression test for the sample AWS certificate and reminder scheduling tests.

## Important limitations and production checklist

- OCR is not certificate verification. HR should compare extracted values with the original file before saving.
- When the certificate image has an expiry label and date, the parser must prefer that labelled date. A validity-period fallback is only a suggestion when no expiry can be extracted, and is explicitly marked as inferred.
- A new Supabase database is independent from the local SQLite database. Existing local data is not silently copied. Export/import employee and certificate data intentionally before cutover.
- A public launch should use HTTPS, unique credentials, a strong `SECRET_KEY`, private Storage, database backups, restricted Supabase database credentials, and an appropriate hosting/service plan.
- This is a practical student/project starter, not a security certification or legal compliance guarantee.

from __future__ import annotations

import csv
import io
import json
import mimetypes
import secrets
import hmac
import os
import re
import shutil
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import quote
from io import BytesIO

import pymupdf as fitz  # PyMuPDF: text extraction and PDF page rendering
import pytesseract
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta
from flask import (
    Flask, abort, flash, g, make_response, redirect, render_template,
    request, send_file, send_from_directory, session, url_for,
)
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash as _werkzeug_generate_password_hash
from werkzeug.utils import secure_filename
import requests
from dotenv import load_dotenv

try:
    import psycopg
    from psycopg import errors as psycopg_errors
except ImportError:  # Optional until a PostgreSQL/Supabase DATABASE_URL is configured.
    psycopg = None
    psycopg_errors = None

def generate_password_hash(password):
    return _werkzeug_generate_password_hash(
        password, method="pbkdf2:sha256"
    )


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
INSTANCE_DIR = BASE_DIR / "instance"
UPLOAD_DIR = INSTANCE_DIR / "uploads"
STAGING_DIR = INSTANCE_DIR / "staging"
DATABASE = INSTANCE_DIR / "certtrack.sqlite3"
# When Supabase's Postgres connection string is present, Postgres becomes the source
# of truth. Local development remains SQLite-first, so the Windows workflow is unchanged.
POSTGRES_DSN = (os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL") or "").strip()
USE_POSTGRES = POSTGRES_DSN.startswith(("postgres://", "postgresql://"))
if POSTGRES_DSN and not USE_POSTGRES:
    raise RuntimeError("SUPABASE_DB_URL/DATABASE_URL must be a PostgreSQL connection string from Supabase's Connect dialog, not the project API URL.")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "certificates")
USE_SUPABASE_STORAGE = bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))

app = Flask(__name__)
_secret = os.environ.get("SECRET_KEY")
if USE_POSTGRES and not _secret:
    raise RuntimeError("Set SECRET_KEY in your deployment environment before connecting CertTrack to Supabase.")
app.secret_key = _secret or "local-development-only-change-before-deploying"
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "1" if USE_POSTGRES else "0") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=int(os.environ.get("SESSION_HOURS", "8")))

for folder in (INSTANCE_DIR, UPLOAD_DIR, STAGING_DIR):
    folder.mkdir(parents=True, exist_ok=True)

if os.environ.get("TESSERACT_CMD"):
    pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]
else:
    # Auto-detect common Windows installer locations so `python app.py` works
    # even when the installer did not add Tesseract to the current PATH.
    _tesseract_candidates = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
    ]
    for _candidate in _tesseract_candidates:
        if _candidate.is_file():
            pytesseract.pytesseract.tesseract_cmd = str(_candidate)
            break

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS locations (
    location_id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_name TEXT NOT NULL UNIQUE,
    region TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS departments (
    department_id INTEGER PRIMARY KEY AUTOINCREMENT,
    department_name TEXT NOT NULL UNIQUE,
    location_id INTEGER NOT NULL REFERENCES locations(location_id)
);
CREATE TABLE IF NOT EXISTS employees (
    employee_id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_code TEXT NOT NULL UNIQUE,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    job_role TEXT NOT NULL DEFAULT '',
    department_id INTEGER NOT NULL REFERENCES departments(department_id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS providers (
    provider_id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    contact_email TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS certification_programs (
    certification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    validity_months INTEGER NOT NULL DEFAULT 12,
    provider_id INTEGER NOT NULL REFERENCES providers(provider_id),
    UNIQUE(title, provider_id)
);
CREATE TABLE IF NOT EXISTS user_accounts (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL UNIQUE REFERENCES employees(employee_id),
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('Admin', 'HR_Staff', 'Manager')),
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS registration_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT NOT NULL,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    job_role TEXT NOT NULL DEFAULT '',
    department_id INTEGER NOT NULL REFERENCES departments(department_id),
    status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Approved', 'Rejected')),
    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    reviewed_by INTEGER REFERENCES user_accounts(user_id),
    assigned_role TEXT CHECK(assigned_role IN ('HR_Staff', 'Manager'))
);
CREATE TABLE IF NOT EXISTS employee_certifications (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(employee_id),
    certification_id INTEGER NOT NULL REFERENCES certification_programs(certification_id),
    issue_date TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    certificate_number TEXT NOT NULL DEFAULT '',
    original_filename TEXT NOT NULL DEFAULT '',
    stored_filename TEXT NOT NULL DEFAULT '',
    ocr_text TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_archived INTEGER NOT NULL DEFAULT 0,
    supersedes_record_id INTEGER REFERENCES employee_certifications(record_id),
    CHECK(expiry_date > issue_date)
);
CREATE TABLE IF NOT EXISTS notification_reminders (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES employee_certifications(record_id),
    scheduled_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Sent', 'Acknowledged')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(record_id, scheduled_date)
);
CREATE TABLE IF NOT EXISTS system_audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    action_type TEXT NOT NULL,
    action_timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    performed_by INTEGER REFERENCES user_accounts(user_id),
    details TEXT NOT NULL DEFAULT ''
);
"""


class HybridRow(dict):
    """A row that supports both SQLite-style integer indexing and named columns."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)




def hybrid_row_factory(cursor):
    def build_row(values):
        description = cursor.description

        if description is None:
            return values

        columns = [column.name for column in description]
        return HybridRow(zip(columns, values))

    return build_row






def translate_postgres_sql(sql: str) -> tuple[str, bool]:
    """Translate the small SQLite SQL dialect used by this app to PostgreSQL."""
    statement = (sql or "").strip().rstrip(";")
    ignore_conflicts = bool(re.match(r"(?is)^INSERT\s+OR\s+IGNORE\s+INTO\b", statement))
    if ignore_conflicts:
        statement = re.sub(r"(?i)^INSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", statement)
        if not re.search(r"(?i)\bON\s+CONFLICT\b", statement):
            statement += " ON CONFLICT DO NOTHING"
    statement = re.sub(r"(?i)\s+COLLATE\s+NOCASE\b", "", statement)
    # Translate positional markers before dialect-specific matching.
    statement = statement.replace("?", "%s")
    statement = re.sub(r"(?i)\bCURRENT_TIMESTAMP\b(?!\s*::)", "CURRENT_TIMESTAMP::text", statement)
    # Approximate SQLite's case-insensitive LIKE and NOCASE duplicate check in PostgreSQL.
    statement = re.sub(r"(?i)\bLIKE\s+%s", "ILIKE %s", statement)
    statement = re.sub(r"(?i)\bcertificate_number\s*=\s*%s", "LOWER(certificate_number) = LOWER(%s)", statement)
    return statement, ignore_conflicts


class PostgresCursor:
    def __init__(self, cursor, insert_row=None, insert_returned=False):
        self._cursor = cursor
        self._insert_row = insert_row
        self._insert_returned = insert_returned

    @property
    def lastrowid(self):
        if self._insert_row:
            return next(iter(self._insert_row.values()))
        return None

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        if self._insert_returned:
            self._insert_returned = False
            return self._insert_row
        return self._cursor.fetchone()

    def fetchall(self):
        rows = self._cursor.fetchall()
        if self._insert_returned:
            self._insert_returned = False
            return ([self._insert_row] if self._insert_row else []) + rows
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class PostgresConnection:
    """Compatibility adapter so existing app routes can run on Supabase Postgres."""
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=()):
        statement, _ = translate_postgres_sql(sql)
        is_insert = bool(re.match(r"(?is)^INSERT\s+INTO\b", statement))
        # Capture generated primary keys in the same way SQLite's cursor.lastrowid does.
        if is_insert and not re.search(r"(?i)\bRETURNING\b", statement):
            statement += " RETURNING *"
        cursor = self._connection.cursor(row_factory=hybrid_row_factory)
        if params:
            cursor.execute(statement, tuple(params))
        else:
            # Passing an empty params tuple asks psycopg to parse literal percent signs as placeholders;
            # several reminder/report queries contain LIKE '%...%' patterns.
            cursor.execute(statement)
        if is_insert and re.search(r"(?i)\bRETURNING\s+\*\s*$", statement):
            inserted = cursor.fetchone()
            return PostgresCursor(cursor, insert_row=inserted, insert_returned=True)
        return PostgresCursor(cursor)

    def executemany(self, sql, seq_of_params):
        statement, _ = translate_postgres_sql(sql)
        # executemany calls in this project are for bulk seed/update data, not ID retrieval.
        statement = re.sub(r"(?i)\s+RETURNING\s+\*\s*$", "", statement)
        cursor = self._connection.cursor(row_factory=hybrid_row_factory)
        cursor.executemany(statement, seq_of_params)
        return PostgresCursor(cursor)

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()

    def close(self):
        return self._connection.close()


def get_db():
    if "db" not in g:
        if USE_POSTGRES:
            if psycopg is None:
                raise RuntimeError("PostgreSQL is configured but psycopg is missing. Install requirements.txt.")
            raw = psycopg.connect(POSTGRES_DSN, sslmode=os.environ.get("PGSSLMODE", "require"), connect_timeout=10)
            g.db = PostgresConnection(raw)
        else:
            g.db = sqlite3.connect(DATABASE)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


@app.before_request
def protect_state_changing_requests():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    if request.method == "POST":
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("_csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
            abort(400, description="The form security token is missing or expired. Refresh the page and try again.")


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def audit(entity_type: str, entity_id: int | None, action: str, details: str = "") -> None:
    user_id = session.get("user_id")
    get_db().execute(
        "INSERT INTO system_audit_log(entity_type, entity_id, action_type, performed_by, details) VALUES (?, ?, ?, ?, ?)",
        (entity_type, entity_id, action, user_id, details[:1000]),
    )


def init_sqlite_db() -> None:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # Lightweight, backward-compatible migrations for databases created by earlier project versions.
    ec_columns = {row[1] for row in conn.execute("PRAGMA table_info(employee_certifications)").fetchall()}
    if "supersedes_record_id" not in ec_columns:
        conn.execute("ALTER TABLE employee_certifications ADD COLUMN supersedes_record_id INTEGER REFERENCES employee_certifications(record_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_employee_cert_expiry ON employee_certifications(expiry_date, is_archived)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_employee_cert_employee ON employee_certifications(employee_id, is_archived)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reminder_status_date ON notification_reminders(status, scheduled_date)")
    if conn.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0] == 0:
        today = date.today()
        conn.execute("INSERT INTO locations(location_name, region) VALUES (?, ?)", ("Hyderabad HQ", "Telangana, India"))
        conn.execute("INSERT INTO locations(location_name, region) VALUES (?, ?)", ("Bengaluru Office", "Karnataka, India"))
        conn.execute("INSERT INTO departments(department_name, location_id) VALUES (?, 1)", ("Engineering",))
        conn.execute("INSERT INTO departments(department_name, location_id) VALUES (?, 1)", ("Data & AI",))
        conn.execute("INSERT INTO departments(department_name, location_id) VALUES (?, 2)", ("Security & Compliance",))
        employees = [
            ("EMP-1001", "Aarav", "Menon", "aarav@example.com", "System Administrator", 1),
            ("EMP-1002", "Diya", "Rao", "diya@example.com", "HR Compliance Officer", 2),
            ("EMP-1003", "Kabir", "Nair", "kabir@example.com", "Engineering Manager", 1),
            ("EMP-1004", "Mira", "Shah", "mira@example.com", "Security Analyst", 3),
            ("EMP-1005", "Ishan", "Das", "ishan@example.com", "Data Engineer", 2),
        ]
        conn.executemany(
            "INSERT INTO employees(employee_code, first_name, last_name, email, job_role, department_id) VALUES (?, ?, ?, ?, ?, ?)",
            employees,
        )
        providers = [("Amazon Web Services", ""), ("Cisco", ""), ("SafeWork Training", ""), ("Microsoft", "")]
        conn.executemany("INSERT INTO providers(provider_name, contact_email) VALUES (?, ?)", providers)
        programs = [
            ("AWS Cloud Practitioner", "Cloud fundamentals certification", 36, 1),
            ("Cisco CCNA", "Networking certification", 36, 2),
            ("First Aid & CPR", "Workplace safety and first aid", 24, 3),
            ("Microsoft Azure Fundamentals", "Cloud fundamentals certification", 24, 4),
        ]
        conn.executemany(
            "INSERT INTO certification_programs(title, description, validity_months, provider_id) VALUES (?, ?, ?, ?)",
            programs,
        )
        accounts = [
            (1, "admin@certtrack.local", generate_password_hash("Admin@123"), "Admin"),
            (2, "hr@certtrack.local", generate_password_hash("HR@123"), "HR_Staff"),
            (3, "manager@certtrack.local", generate_password_hash("Manager@123"), "Manager"),
        ]
        conn.executemany("INSERT INTO user_accounts(employee_id, username, password_hash, role) VALUES (?, ?, ?, ?)", accounts)
        sample_records = [
            (1, 1, (today - timedelta(days=90)).isoformat(), (today + timedelta(days=365)).isoformat(), "AWS-CP-1042"),
            (2, 3, (today - timedelta(days=350)).isoformat(), (today + timedelta(days=18)).isoformat(), "CPR-8871"),
            (4, 2, (today - timedelta(days=760)).isoformat(), (today - timedelta(days=22)).isoformat(), "CCNA-2024-66"),
            (5, 4, (today - timedelta(days=120)).isoformat(), (today + timedelta(days=65)).isoformat(), "AZ-900-5418"),
        ]
        conn.executemany(
            "INSERT INTO employee_certifications(employee_id, certification_id, issue_date, expiry_date, certificate_number) VALUES (?, ?, ?, ?, ?)",
            sample_records,
        )
        for record_id, expiry in [(1, sample_records[0][3]), (2, sample_records[1][3]), (3, sample_records[2][3]), (4, sample_records[3][3])]:
            scheduled = (date.fromisoformat(expiry) - timedelta(days=30)).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO notification_reminders(record_id, scheduled_date, status) VALUES (?, ?, 'Pending')",
                (record_id, scheduled),
            )
        conn.execute("INSERT INTO system_audit_log(entity_type, entity_id, action_type, details) VALUES ('SYSTEM', 1, 'SEED', 'Demo environment initialized')")
        conn.commit()
    conn.close()


POSTGRES_SCHEMA = re.sub(r"(?im)^\s*PRAGMA foreign_keys = ON;\s*", "", SCHEMA)
POSTGRES_SCHEMA = POSTGRES_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
POSTGRES_SCHEMA = POSTGRES_SCHEMA.replace("provider_name TEXT NOT NULL UNIQUE COLLATE NOCASE", "provider_name CITEXT NOT NULL UNIQUE")
POSTGRES_SCHEMA = POSTGRES_SCHEMA.replace("title TEXT NOT NULL COLLATE NOCASE", "title CITEXT NOT NULL")
POSTGRES_SCHEMA = POSTGRES_SCHEMA.replace("username TEXT NOT NULL UNIQUE COLLATE NOCASE", "username CITEXT NOT NULL UNIQUE")
POSTGRES_SCHEMA = POSTGRES_SCHEMA.replace("email TEXT NOT NULL UNIQUE", "email CITEXT NOT NULL UNIQUE")


def init_postgres_db() -> None:
    if psycopg is None:
        raise RuntimeError("Supabase/PostgreSQL is configured but psycopg is not installed.")
    raw = psycopg.connect(POSTGRES_DSN, sslmode=os.environ.get("PGSSLMODE", "require"), connect_timeout=10)
    conn = PostgresConnection(raw)
    lock_name = "certtrack-schema-bootstrap"
    try:
        # Gunicorn starts multiple workers; serialize DDL and first-admin bootstrap.
        conn.execute("SELECT pg_advisory_lock(hashtext(?))", (lock_name,)).fetchone()
        # CITEXT is required for case-insensitive provider/title/email/username columns.
        # Create it explicitly before any table declaration references the type.
        conn.execute("CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public")
        for statement in POSTGRES_SCHEMA.split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(statement)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_employee_cert_expiry ON employee_certifications(expiry_date, is_archived)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_employee_cert_employee ON employee_certifications(employee_id, is_archived)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reminder_status_date ON notification_reminders(status, scheduled_date)")
        # The Flask service uses the private DB connection and enforces role checks in every route.
        # Enable RLS so Supabase's public Data API does not expose these tables by default.
        for table in ("locations", "departments", "employees", "providers", "certification_programs", "user_accounts", "registration_requests", "employee_certifications", "notification_reminders", "system_audit_log"):
            conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        count = conn.execute("SELECT COUNT(*) FROM user_accounts").fetchone()[0]
        if count == 0:
            username = os.environ.get("INITIAL_ADMIN_USERNAME", "").strip()
            password = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
            if not username or not password:
                raise RuntimeError("The Supabase database is empty. Set INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD before starting CertTrack.")
            if len(password) < 12:
                raise RuntimeError("INITIAL_ADMIN_PASSWORD must contain at least 12 characters.")
            first_name = os.environ.get("INITIAL_ADMIN_FIRST_NAME", "Workspace").strip() or "Workspace"
            last_name = os.environ.get("INITIAL_ADMIN_LAST_NAME", "Admin").strip() or "Admin"
            conn.execute("INSERT INTO locations(location_name, region) VALUES (?, ?)", ("Main Office", "Configure in Administration"))
            location_id = conn.execute("SELECT location_id FROM locations WHERE location_name = ?", ("Main Office",)).fetchone()[0]
            conn.execute("INSERT INTO departments(department_name, location_id) VALUES (?, ?)", ("Administration", location_id))
            department_id = conn.execute("SELECT department_id FROM departments WHERE department_name = ?", ("Administration",)).fetchone()[0]
            conn.execute("INSERT INTO employees(employee_code, first_name, last_name, email, job_role, department_id) VALUES (?, ?, ?, ?, ?, ?)", ("ADMIN-0001", first_name, last_name, username, "System Administrator", department_id))
            employee_id = conn.execute("SELECT employee_id FROM employees WHERE employee_code = ?", ("ADMIN-0001",)).fetchone()[0]
            conn.execute("INSERT INTO user_accounts(employee_id, username, password_hash, role) VALUES (?, ?, ?, 'Admin')", (employee_id, username, generate_password_hash(password)))
            conn.execute("INSERT INTO providers(provider_name, contact_email) VALUES (?, ?)", ("Amazon Web Services", ""))
            conn.execute("INSERT INTO providers(provider_name, contact_email) VALUES (?, ?)", ("Microsoft", ""))
            conn.execute("INSERT INTO providers(provider_name, contact_email) VALUES (?, ?)", ("Cisco", ""))
            aws_id = conn.execute("SELECT provider_id FROM providers WHERE provider_name = ?", ("Amazon Web Services",)).fetchone()[0]
            microsoft_id = conn.execute("SELECT provider_id FROM providers WHERE provider_name = ?", ("Microsoft",)).fetchone()[0]
            cisco_id = conn.execute("SELECT provider_id FROM providers WHERE provider_name = ?", ("Cisco",)).fetchone()[0]
            conn.execute("INSERT INTO certification_programs(title, description, validity_months, provider_id) VALUES (?, ?, ?, ?)", ("AWS Certified Solutions Architect – Associate", "Cloud architecture certification", 36, aws_id))
            conn.execute("INSERT INTO certification_programs(title, description, validity_months, provider_id) VALUES (?, ?, ?, ?)", ("Microsoft Azure Fundamentals", "Cloud fundamentals certification", 24, microsoft_id))
            conn.execute("INSERT INTO certification_programs(title, description, validity_months, provider_id) VALUES (?, ?, ?, ?)", ("Cisco CCNA", "Networking certification", 36, cisco_id))
            conn.execute("INSERT INTO system_audit_log(entity_type, entity_id, action_type, details) VALUES ('SYSTEM', 1, 'BOOTSTRAP', 'Initial administrator created from environment configuration')")
            conn.commit()
            app.logger.warning("Created initial CertTrack administrator for %s. Change its password after first login.", username)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext(?))", (lock_name,)).fetchone()
            conn.commit()
        except Exception:
            pass
        conn.close()


def init_db() -> None:
    if USE_POSTGRES:
        init_postgres_db()
    else:
        init_sqlite_db()


init_db()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute(
        """SELECT u.user_id, u.username, u.role, e.employee_id, e.employee_code,
                  e.first_name, e.last_name, e.email, e.department_id, d.department_name
           FROM user_accounts u JOIN employees e ON e.employee_id = u.employee_id
           JOIN departments d ON d.department_id = e.department_id
           WHERE u.user_id = ? AND u.active = 1""",
        (user_id,),
    ).fetchone()


@app.context_processor
def inject_globals():
    user = current_user()
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return {"current_user": user, "can_manage": bool(user and user["role"] in ("Admin", "HR_Staff")), "today_iso": date.today().isoformat(), "csrf_token": session["_csrf_token"], "show_demo_logins": (not USE_POSTGRES and os.environ.get("SHOW_DEMO_LOGINS", "1") == "1")}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login", next=request.path))
            if user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.template_filter("to_date")
def to_date_filter(value: str):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return date.today()


def status_for(expiry_str: str) -> str:
    try:
        expiry = date.fromisoformat(expiry_str)
    except (ValueError, TypeError):
        return "Unknown"
    remaining = (expiry - date.today()).days
    if remaining < 0:
        return "Expired"
    if remaining <= 30:
        return "Expiring Soon"
    return "Valid"


def ensure_reminders_for_record(record_id: int, reset: bool = False) -> None:
    """Idempotently queue in-app reminders 30, 7 and 1 day before expiry.

    When a deadline is already inside one of those windows, that reminder is due
    immediately. This is a page-request based queue, not an email/background worker.
    """
    db = get_db()
    row = db.execute(
        "SELECT expiry_date, is_archived FROM employee_certifications WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    if not row:
        return
    if reset:
        db.execute("DELETE FROM notification_reminders WHERE record_id = ?", (record_id,))
    if row["is_archived"]:
        db.commit()
        return
    try:
        expiry = date.fromisoformat(row["expiry_date"])
    except (ValueError, TypeError):
        return
    today = date.today()
    if expiry < today:
        # Expired items stay visible on the dashboard; do not keep producing reminders for them.
        db.execute(
            "DELETE FROM notification_reminders WHERE record_id = ? AND status = 'Pending'",
            (record_id,),
        )
        db.commit()
        return

    # Keep the original milestone dates even when the certificate was entered
    # late. A missed 30-day alert should be due immediately, but its scheduled
    # date must not be rewritten to today's date on every page view (which would
    # create a fresh due reminder every day). INSERT OR IGNORE below makes each
    # milestone idempotent for a given certificate.
    target_dates = {
        (expiry - timedelta(days=days_before)).isoformat()
        for days_before in (30, 7, 1)
    }
    # Remove stale pending reminder dates left over from an earlier deadline.
    existing = db.execute(
        "SELECT notification_id, scheduled_date FROM notification_reminders WHERE record_id = ? AND status = 'Pending'",
        (record_id,),
    ).fetchall()
    for reminder in existing:
        if reminder["scheduled_date"] < today.isoformat() and reminder["scheduled_date"] not in target_dates:
            db.execute("DELETE FROM notification_reminders WHERE notification_id = ?", (reminder["notification_id"],))
    for scheduled in sorted(target_dates):
        db.execute(
            "INSERT OR IGNORE INTO notification_reminders(record_id, scheduled_date, status) VALUES (?, ?, 'Pending')",
            (record_id, scheduled),
        )
    db.commit()


def ensure_all_reminders() -> None:
    """Generate/reconcile reminders for every current, non-archived certificate."""
    rows = get_db().execute(
        "SELECT record_id FROM employee_certifications WHERE is_archived = 0"
    ).fetchall()
    for row in rows:
        ensure_reminders_for_record(row["record_id"])


def scoped_reminder_query(status_filter: str = ""):
    user = current_user()
    sql = """SELECT nr.*, ec.expiry_date, ec.record_id, ec.employee_id, ec.is_archived,
                    e.employee_code, e.first_name, e.last_name, e.department_id,
                    d.department_name, cp.title AS certification_title, p.provider_name
             FROM notification_reminders nr
             JOIN employee_certifications ec ON ec.record_id = nr.record_id
             JOIN employees e ON e.employee_id = ec.employee_id
             JOIN departments d ON d.department_id = e.department_id
             JOIN certification_programs cp ON cp.certification_id = ec.certification_id
             JOIN providers p ON p.provider_id = cp.provider_id
             WHERE ec.is_archived = 0"""
    params = []
    if user and user["role"] == "Manager":
        sql += " AND e.department_id = ?"
        params.append(user["department_id"])
    if status_filter in ("Pending", "Acknowledged", "Sent"):
        sql += " AND nr.status = ?"
        params.append(status_filter)
    sql += " ORDER BY CASE WHEN nr.status = 'Pending' AND nr.scheduled_date <= ? THEN 0 ELSE 1 END, nr.scheduled_date ASC, nr.notification_id DESC"
    params.append(date.today().isoformat())
    return get_db().execute(sql, params).fetchall()


def scoped_cert_query(include_archived=False, record_id=None):
    """Return visible certificates, enforcing manager department scope."""
    user = current_user()
    sql = """SELECT ec.*, e.employee_code, e.first_name, e.last_name, e.email,
                    e.department_id, d.department_name, cp.title AS certification_title,
                    p.provider_name, l.location_name
             FROM employee_certifications ec
             JOIN employees e ON e.employee_id = ec.employee_id
             JOIN departments d ON d.department_id = e.department_id
             JOIN locations l ON l.location_id = d.location_id
             JOIN certification_programs cp ON cp.certification_id = ec.certification_id
             JOIN providers p ON p.provider_id = cp.provider_id"""
    clauses, params = [], []
    if not include_archived:
        clauses.append("ec.is_archived = 0")
    if user and user["role"] == "Manager":
        clauses.append("e.department_id = ?")
        params.append(user["department_id"])
    if record_id is not None:
        clauses.append("ec.record_id = ?")
        params.append(record_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ec.expiry_date ASC, ec.record_id DESC"
    rows = get_db().execute(sql, params).fetchall()
    return rows


def create_or_get_provider(name: str) -> int:
    db = get_db()
    name = (name or "Unknown Provider").strip()[:160] or "Unknown Provider"
    existing = db.execute("SELECT provider_id FROM providers WHERE provider_name = ? COLLATE NOCASE", (name,)).fetchone()
    if existing:
        return existing["provider_id"]
    cur = db.execute("INSERT INTO providers(provider_name) VALUES (?)", (name,))
    return cur.lastrowid


def create_or_get_program(title: str, provider_id: int) -> int:
    db = get_db()
    title = (title or "Untitled Certification").strip()[:200] or "Untitled Certification"
    existing = db.execute(
        "SELECT certification_id FROM certification_programs WHERE title = ? COLLATE NOCASE AND provider_id = ?",
        (title, provider_id),
    ).fetchone()
    if existing:
        return existing["certification_id"]
    provider = db.execute("SELECT provider_name FROM providers WHERE provider_id = ?", (provider_id,)).fetchone()
    provider_name = provider["provider_name"].casefold() if provider else ""
    # AWS credentials have a standard three-year validity period. Other programs
    # retain a conservative demo default until an administrator configures them.
    default_validity = 36 if ("amazon web services" in provider_name or re.search(r"\baws\b", provider_name)) else 12
    cur = db.execute(
        "INSERT INTO certification_programs(title, description, validity_months, provider_id) VALUES (?, '', ?, ?)",
        (title, default_validity, provider_id),
    )
    return cur.lastrowid


def parse_certificate_date(token: str, *, end_of_month: bool = False) -> date | None:
    """Parse full dates and month/year dates printed on certificates.

    A month/year-only issue date is represented as the first day of that month;
    a month/year-only expiry date is represented as the last day of that month.
    The review screen still requires a human to verify OCR output before saving.
    """
    token = token.strip().replace("\u2013", "-").replace("\u2014", "-")
    token = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", token, flags=re.IGNORECASE)
    month_year = re.fullmatch(
        r"(?i)\s*(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?[\s,./-]+(\d{4})\s*",
        token,
    )
    if month_year:
        try:
            parsed = date_parser.parse(f"{month_year.group(1)} 1 {month_year.group(2)}", dayfirst=False).date()
            if end_of_month:
                return (parsed.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            return parsed
        except (ValueError, OverflowError, TypeError):
            return None
    try:
        # Certificates in India commonly use DD/MM/YYYY; ISO dates remain unambiguous.
        return date_parser.parse(token, dayfirst=True, fuzzy=False, default=datetime(2000, 1, 1)).date()
    except (ValueError, OverflowError, TypeError):
        return None


DATE_TOKEN = re.compile(
    r"(?i)(?:\b\d{4}[./-]\d{1,2}[./-]\d{1,2}\b|"
    r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|"
    r"\b\d{1,2}(?:st|nd|rd|th)?[- /]+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?[- ,]+\d{4}\b|"
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?[- /]+\d{1,2}(?:st|nd|rd|th)?[,]?[- /]+\d{4}\b|"
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?[\s,./-]+\d{4}\b)"
)

ISSUE_LABEL = re.compile(
    r"(?i)\b(?:issue\s*date|date\s*of\s*issue|date\s*issued|issued(?:\s+on|\s+date)?|"
    r"issuance\s*date|certification\s*date|date\s*of\s*certification|date\s*awarded|awarded\s*on|award\s*date|date\s*of\s*award|"
    r"completion\s*date|completed\s*on|date\s*of\s*completion|date\s*earned|earned\s*on|"
    r"valid\s*from|effective\s*date|start\s*date)\b"
)
EXPIRY_LABEL = re.compile(
    r"(?i)\b(?:date\s+of\s+(?:expiry|expiration|expiring)|"
    r"expir(?:y|es|ation|e)\s*(?:date|on)?|expires?\s*(?:on|by)?|"
    r"valid\s*(?:until|through|thru|till|till|to|up\s+to)|"
    r"good\s*(?:through|until|till)|validity\s*(?:end|date|until|till)|"
    r"renew(?:al)?\s*(?:date|by|before)|recertification\s*(?:due|date)|"
    r"recertify\s*(?:by|before))\b"
)


def dates_in(text: str) -> list[tuple[date, int, int, str]]:
    found = []
    for match in DATE_TOKEN.finditer(text or ""):
        parsed = parse_certificate_date(match.group(0))
        if parsed and 1980 <= parsed.year <= 2100:
            found.append((parsed, match.start(), match.end(), match.group(0)))
    return found


def date_near_label(text: str, label_regex: re.Pattern) -> date | None:
    """Find the date associated with a label, whether the date is before or after it.

    Certificates commonly place the date above its caption (for example, a date
    followed by "Expiration Date"), while PDF text layers often use "Expiry Date:
    2027-02-24". Prefer same-line dates, then nearby lines, while checking both
    sides of the label.
    """
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        for match in label_regex.finditer(line):
            same_line = list(DATE_TOKEN.finditer(line))
            if same_line:
                nearest = min(same_line, key=lambda item: abs(item.start() - match.start()))
                parsed = parse_certificate_date(nearest.group(0), end_of_month=(label_regex is EXPIRY_LABEL))
                if parsed and 1980 <= parsed.year <= 2100:
                    return parsed

            # A caption is often printed directly below the date on certificates.
            # Check the immediately preceding line first; if there is no date there,
            # look at the following lines (common in form-style PDFs).
            for distance in (1, 2):
                # For expiry labels, prefer a date following the label.
                # For issue labels, preserve the original preceding-first behavior.
                directions = (
                    (i + distance, True, i - distance, False)
                    if label_regex is EXPIRY_LABEL
                    else (i - distance, False, i + distance, True)
                )
                first_index, first_token, second_index, second_token = directions
                for line_index, token_index in (
                    (first_index, first_token),
                    (second_index, second_token),
                ):
                    if 0 <= line_index < len(lines):
                        tokens = list(DATE_TOKEN.finditer(lines[line_index]))
                        if tokens:
                            token = tokens[0] if token_index else tokens[-1]
                            parsed = parse_certificate_date(
                                token.group(0),
                                end_of_month=(label_regex is EXPIRY_LABEL),
                            )
                            if parsed and 1980 <= parsed.year <= 2100:
                                return parsed

    # PDF text layers sometimes collapse positions into a long line. Search the
    # nearest preceding date before looking forward so a caption below its date
    # does not accidentally bind to the next certificate date.
    source = text or ""
    for match in label_regex.finditer(source):
        before = source[max(0, match.start() - 100):match.start()]
        tokens_before = list(DATE_TOKEN.finditer(before))
        if tokens_before:
            parsed = parse_certificate_date(tokens_before[-1].group(0), end_of_month=(label_regex is EXPIRY_LABEL))
            if parsed and 1980 <= parsed.year <= 2100:
                return parsed
        tail = source[match.end():match.end() + 100]
        tokens_after = list(DATE_TOKEN.finditer(tail))
        if tokens_after:
            parsed = parse_certificate_date(tokens_after[0].group(0), end_of_month=(label_regex is EXPIRY_LABEL))
            if parsed and 1980 <= parsed.year <= 2100:
                return parsed
    return None


def extract_certificate_fields(text: str) -> dict:
    compact = re.sub(r"[ \t]+", " ", text or "")
    issue = date_near_label(text, ISSUE_LABEL)
    expiry = date_near_label(text, EXPIRY_LABEL)
    all_dates = dates_in(text)

    # Handle common wording such as "Valid from 1 Jan 2024 to 1 Jan 2027".
    # The range is only used when both dates are present in the same nearby text.
    valid_range = re.search(r"(?is)\bvalid\s+from\b.{0,180}?\b(?:to|through|until|till)\b.{0,100}", text or "")
    if valid_range:
        range_dates = [d for d, *_ in dates_in(valid_range.group(0))]
        if len(range_dates) >= 2:
            issue = issue or range_dates[0]
            expiry = expiry or range_dates[1]

    # Use unique calendar dates so duplicate matches from a PDF text layer and OCR
    # do not confuse selection. When labels were poorly recognized, certificate
    # layouts often put the issue date first and expiry date second.
    unique_dates = sorted({item[0] for item in all_dates})
    issue_label_present = bool(ISSUE_LABEL.search(text or ""))
    expiry_label_present = bool(EXPIRY_LABEL.search(text or ""))

    # If labels point to an impossible ordering (for example, because OCR placed
    # a caption below the wrong date), prefer the chronological pair actually seen.
    if issue and expiry and expiry <= issue and len(unique_dates) >= 2:
        issue, expiry = unique_dates[0], unique_dates[-1]
    if issue is None and len(unique_dates) >= 2:
        issue = unique_dates[0]
    if expiry is None and issue is not None:
        later_dates = [value for value in unique_dates if value > issue]
        if later_dates:
            expiry = max(later_dates)
    if issue is None and expiry is not None and len(unique_dates) >= 2:
        earlier_dates = [value for value in unique_dates if value < expiry]
        if earlier_dates:
            issue = min(earlier_dates)

    # Crucially, never treat a single unlabelled date as the issue date. That was
    # the bug behind inferring 2022 from a certificate whose only OCR-detected date
    # was actually its printed 2019 expiration date. An inference may be attempted
    # only when the issue date is explicitly labelled and no expiry label is seen.
    allow_expiry_inference = bool(
        issue and issue_label_present and not expiry_label_present and len(unique_dates) == 1
    )
    lines = [re.sub(r"\s+", " ", x).strip(" :|\t") for x in compact.splitlines()]
    lines = [line for line in lines if line]
    title = ""
    # Prefer an explicitly named AWS credential over surrounding prose.
    aws_title_match = re.search(r"(?im)\bAWS\s+Certified\s+([^\r\n]{3,120})", text or "")
    if aws_title_match:
        aws_suffix = re.sub(r"\s+", " ", aws_title_match.group(1)).strip(" .,:;|-")
        title = ("AWS Certified " + aws_suffix).strip()[:200]
    title_terms = ("certificate", "certification", "certified", "professional", "specialization", "course", "training", "cloud practitioner", "ccna", "azure fundamentals", "data analytics")
    title_candidates = [
        line for line in lines
        if any(term in line.lower() for term in title_terms)
        and len(line) >= 5 and len(line) <= 140
        and not re.search(r"(?i)issued|issue date|certification date|date of certification|expiry|expiration|valid (?:until|till|to|through)|this is to certify|certificate id|certificate number|expires?|expir|has successfully completed|successfully completed|is recognized|requirements and", line)
    ]
    if title_candidates and not title:
        # Prefer a descriptive mid-length line over a generic heading.
        title = max(title_candidates, key=lambda x: (len(x.split()) >= 3, min(len(x), 100)))
        title = re.sub(r"^[^A-Za-z0-9]+", "", title)[:200]

    provider_names = [
        ("Amazon Web Services", ["amazon web services", "aws"]),
        ("Microsoft", ["microsoft"]), ("Google", ["google"]),
        ("Cisco", ["cisco"]), ("CompTIA", ["comptia"]),
        ("IBM", ["ibm"]), ("Oracle", ["oracle"]),
        ("Red Hat", ["red hat"]), ("Coursera", ["coursera"]),
        ("Udemy", ["udemy"]), ("LinkedIn Learning", ["linkedin learning"]),
        ("EC-Council", ["ec-council", "eccouncil"]), ("Pearson", ["pearson"]),
        ("Scrum.org", ["scrum.org"]), ("Kaggle", ["kaggle"]),
    ]
    lower = compact.lower()
    provider = ""
    for canonical, aliases in provider_names:
        if any(re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", lower) for alias in aliases):
            provider = canonical
            break
    if not provider:
        m = re.search(r"(?im)^\s*(?:issued\s+by|provider|issuing\s+organization)\s*[:\-]\s*(.{2,100})$", text or "")
        if m:
            provider = m.group(1).strip()[:160]

    cert_no = ""
    m = re.search(r"(?i)\b(?:certificate|credential|certification)\s*(?:id|no\.?|number|#)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9_\-/]{3,40})", compact)
    if m:
        cert_no = m.group(1).strip(" .,:;")[:80]
    else:
        # Common layout: "Certificate AWS-ASA-32001" with no explicit ID label.
        m = re.search(r"(?i)\bcertificate\s+((?=[A-Z0-9_/-]*\d)[A-Z0-9][A-Z0-9_/-]{3,39})\b", compact)
        if m:
            cert_no = m.group(1).strip(" .,:;")[:80]

    employee_name = ""
    m = re.search(r"(?is)(?:this\s+is\s+to\s+certify\s+that|awarded\s+to|presented\s+to|issued\s+to|successfully\s+completed\s+by)\s*[:\-]?\s*([A-Z][A-Za-z.' -]{2,60})", text or "")
    if m:
        employee_name = re.split(r"\n|\b(?:has|for|on|in recognition)\b", m.group(1), maxsplit=1, flags=re.IGNORECASE)[0].strip(" .,:-")[:100]
    if not employee_name:
        # Some certificate layouts print the recipient as a standalone line.
        blocked = ("solutions architect", "associate level", "amazon web services", "aws certified", "has successfully", "certificate", "director", "training", "issue", "date", "expir", "validity", "february", "january", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
        for line_index, line in enumerate(lines):
            nearby = lines[max(0, line_index - 1):min(len(lines), line_index + 2)]
            # OCR may turn a caption such as "Certification Issue Date" into
            # something that resembles a person's name. Do not use lines directly
            # adjacent to date values as a fallback recipient name.
            date_adjacent = any(DATE_TOKEN.search(candidate) for candidate in nearby)
            if (re.fullmatch(r"[A-Z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+){1,2}", line)
                    and not date_adjacent
                    and not any(term in line.casefold() for term in blocked)
                    and not re.search(r"\d|[=<>]", line)):
                employee_name = line[:100]
                break

    return {
        "title": title,
        "provider": provider,
        "issue_date": issue.isoformat() if issue else "",
        "expiry_date": expiry.isoformat() if expiry else "",
        "certificate_number": cert_no,
        "employee_name": employee_name,
        "_allow_expiry_inference": allow_expiry_inference,
    }


def infer_expiry_when_not_printed(fields: dict) -> dict:
    """Prefill an expiry date only when a reliable validity rule is known.

    The inferred date is explicitly labelled in the review UI. It is not claimed
    to have been read from the certificate. AWS certifications normally have a
    three-year validity period, so a missing AWS expiry is prefilled from the
    parsed issue date; HR must verify current status/recertification before save.
    """
    if (fields.get("expiry_date") or not fields.get("issue_date")
            or not fields.get("_allow_expiry_inference", False)):
        return fields

    try:
        issue = date.fromisoformat(fields["issue_date"])
    except (ValueError, TypeError):
        return fields

    title = (fields.get("title") or "").strip()
    provider_name = (fields.get("provider") or "").strip()
    title_norm = title.casefold()
    provider_norm = provider_name.casefold()

    # Detect AWS from either field. Do this BEFORE the database catalog lookup so
    # the known 36-month AWS rule works even for a brand-new/unlisted program.
    is_aws = (
        "amazon web services" in provider_norm
        or bool(re.search(r"\baws\b", provider_name, re.IGNORECASE))
        or bool(re.search(r"\baws\s+certified\b", title, re.IGNORECASE))
        or ("amazon" in provider_norm and "solutions architect" in title_norm)
    )
    if is_aws:
        expiry = issue + relativedelta(months=36)
        if expiry > issue:
            fields["expiry_date"] = expiry.isoformat()
            fields["_expiry_note"] = (
                "No expiry date was found in the OCR text. This date is inferred "
                "as three years from the issue date using the standard AWS "
                "certification validity period. Verify the credential and any "
                "recertification in the AWS Certification account before saving."
            )
        return fields

    # For non-AWS programs, use only a validity period explicitly configured in
    # the certification catalog; do not invent a generic expiry date.
    if not title or not provider_name:
        return fields
    configured = get_db().execute(
        """SELECT cp.validity_months FROM certification_programs cp
           JOIN providers p ON p.provider_id = cp.provider_id
           WHERE cp.title = ? COLLATE NOCASE AND p.provider_name = ? COLLATE NOCASE
           LIMIT 1""",
        (title, provider_name),
    ).fetchone()
    if not configured:
        return fields
    try:
        validity_months = int(configured["validity_months"] or 0)
    except (ValueError, TypeError, KeyError):
        validity_months = 0
    if validity_months <= 0:
        return fields

    expiry = issue + relativedelta(months=validity_months)
    if expiry > issue:
        fields["expiry_date"] = expiry.isoformat()
        fields["_expiry_note"] = (
            f"No expiry date was found in the OCR text. This date is inferred "
            f"using the {validity_months}-month validity configured for this "
            "certification program. Verify it against the original credential "
            "before saving."
        )
    return fields


def ocr_file(path: Path) -> str:
    """Read text from a digital or scanned PDF, or a supported image."""
    ext = path.suffix.lower()
    chunks = []
    if ext == ".pdf":
        try:
            doc = fitz.open(path)
        except Exception as exc:
            raise ValueError("This PDF could not be opened. Try exporting it again as a PDF or image.") from exc
        if doc.page_count > 8:
            doc.close()
            raise ValueError("Please upload a certificate of 8 pages or fewer.")
        try:
            for page in doc:
                text_layer = page.get_text("text").strip()
                # Prefer embedded text, but also OCR PDFs whose image/graphic layer may contain
                # a date missing from the embedded text layer.
                should_ocr = len(text_layer) < 35
                if not should_ocr:
                    visible_dates = len(DATE_TOKEN.findall(text_layer))
                    has_expiry_label = bool(EXPIRY_LABEL.search(text_layer))
                    should_ocr = visible_dates < 2 or not has_expiry_label
                if should_ocr:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
                    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    ocr_text = run_tesseract(image).strip()
                    # Keep both sources: the parser can use the text layer and OCR text together.
                    page_text = "\n".join(part for part in (text_layer, ocr_text) if part)
                else:
                    page_text = text_layer
                if page_text.strip():
                    chunks.append(page_text.strip())
        finally:
            doc.close()
    else:
        try:
            with Image.open(path) as img:
                img.load()
                if img.width * img.height > 35_000_000:
                    raise ValueError("The image is too large to process. Resize it and try again.")
                chunks.append(run_tesseract(img.copy()))
        except UnidentifiedImageError as exc:
            raise ValueError("The uploaded file does not look like a supported image.") from exc
    joined = "\n\n".join(chunks).strip()
    if not joined:
        raise ValueError("No text could be read. Try a clearer scan or enter the certificate details manually.")
    return joined[:30000]


def run_tesseract(image: Image.Image) -> str:
    """Run OCR using a limited number of configurations and per-pass timeouts."""
    try:
        gray = ImageOps.autocontrast(ImageOps.grayscale(image))

        # Upscale small certificate images to improve recognition of fine date labels.
        if gray.width < 1800:
            ratio = 1800 / max(gray.width, 1)
            gray = gray.resize(
                (1800, max(1, int(gray.height * ratio))),
                Image.Resampling.LANCZOS,
            )

        threshold = gray.point(lambda px: 255 if px >= 175 else 0)
        sharpened = gray.filter(
            ImageFilter.UnsharpMask(radius=2, percent=160, threshold=3)
        )

        candidates: list[tuple[float, str]] = []
        configurations = [
            (gray, 11),
            (sharpened, 11),
            (gray, 6),
            (threshold, 11),
            (threshold, 6),
        ]

        for image_variant, psm in configurations:
            app.logger.info(
                "Tesseract OCR started: PSM=%s, image=%sx%s",
                psm,
                image_variant.width,
                image_variant.height,
            )
            try:
                output = pytesseract.image_to_string(
                    image_variant,
                    config=f"--oem 3 --psm {psm}",
                    timeout=15,
                ).strip()
            except RuntimeError:
                app.logger.warning(
                    "Tesseract OCR timed out for PSM=%s; trying the next configuration.",
                    psm,
                )
                continue

            if not output:
                continue

            distinct_dates = {item[0] for item in dates_in(output)}
            score = min(len(distinct_dates), 5) * 20
            score += 12 if ISSUE_LABEL.search(output) else 0
            score += 12 if EXPIRY_LABEL.search(output) else 0
            score += min(len(re.findall(r"[A-Za-z]{3,}", output)), 40) * 0.15
            score += min(
                len(
                    re.findall(
                        r"(?i)\b(?:certification\s+date|valid\s+till|valid\s+until|expiry\s+date|expiration\s+date)\b",
                        output,
                    )
                ),
                3,
            ) * 5

            candidates.append((score, output))
            app.logger.info(
                "Tesseract OCR pass completed: PSM=%s, distinct_dates=%s, score=%.2f",
                psm,
                len(distinct_dates),
                score,
            )

            # Stop early only when both date labels and at least two distinct dates are found.
            if (
                len(distinct_dates) >= 2
                and ISSUE_LABEL.search(output)
                and EXPIRY_LABEL.search(output)
            ):
                return output

        if not candidates:
            app.logger.warning("Tesseract OCR produced no usable text.")
            return ""

        return max(candidates, key=lambda item: item[0])[1]

    except pytesseract.TesseractNotFoundError as exc:
        raise ValueError(
            "Tesseract OCR was not found. Install Tesseract and add it to PATH, "
            "or set TESSERACT_CMD in your environment. See README.md."
        ) from exc
    except pytesseract.TesseractError as exc:
        raise ValueError(
            "Tesseract could not read this file. Try a clearer scan or enter "
            "the certificate details manually."
        ) from exc


def persist_certificate_file(staged_path: Path, stored_key: str) -> str:
    """Persist a reviewed certificate locally or to a private Supabase Storage bucket.

    When PostgreSQL is configured, refuse to silently save a new certificate only
    on the container filesystem. Hosted container filesystems may be ephemeral;
    require durable Storage to avoid losing employee certificates on redeploy.
    """
    if not USE_SUPABASE_STORAGE:
        if USE_POSTGRES:
            raise ValueError(
                "Supabase Storage is not configured. Create a private 'certificates' bucket, "
                "then set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY before saving an uploaded file."
            )
        shutil.move(str(staged_path), str(UPLOAD_DIR / stored_key))
        return stored_key
    bucket = quote(SUPABASE_STORAGE_BUCKET, safe="")
    object_path = quote(stored_key, safe="/")
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{object_path}"
    content_type = mimetypes.guess_type(staged_path.name)[0] or "application/octet-stream"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": content_type,
        "x-upsert": "false",
    }
    try:
        response = requests.post(url, data=staged_path.read_bytes(), headers=headers, timeout=45)
        if response.status_code not in (200, 201):
            app.logger.error("Supabase Storage upload failed (%s): %s", response.status_code, response.text[:500])
            raise ValueError("The certificate could not be uploaded to Supabase Storage. Confirm the private bucket exists and the service key is correct.")
    except requests.RequestException as exc:
        app.logger.exception("Supabase Storage request failed")
        raise ValueError("Unable to reach Supabase Storage. Check SUPABASE_URL and the network, then try again.") from exc
    staged_path.unlink(missing_ok=True)
    return f"remote:{stored_key}"


def fetch_certificate_file(stored_filename: str):
    """Return (bytes, mimetype) for a local file or a private Supabase Storage object."""
    if stored_filename.startswith("remote:"):
        if not USE_SUPABASE_STORAGE:
            abort(503, description="This certificate is stored in Supabase Storage. Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY to retrieve it.")
        key = stored_filename.removeprefix("remote:")
        bucket = quote(SUPABASE_STORAGE_BUCKET, safe="")
        object_path = quote(key, safe="/")
        url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{object_path}"
        headers = {"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}", "apikey": SUPABASE_SERVICE_ROLE_KEY}
        try:
            response = requests.get(url, headers=headers, timeout=45)
            if response.status_code != 200:
                app.logger.error("Supabase Storage download failed (%s): %s", response.status_code, response.text[:500])
                abort(502, description="The certificate file could not be retrieved from Supabase Storage.")
            return response.content, response.headers.get("Content-Type", "application/octet-stream")
        except requests.RequestException as exc:
            app.logger.exception("Supabase Storage download request failed")
            abort(502, description="Unable to contact Supabase Storage.")
    path = UPLOAD_DIR / Path(stored_filename).name
    if not path.exists():
        abort(404)
    return path.read_bytes(), (mimetypes.guess_type(path.name)[0] or "application/octet-stream")

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_employees_for_user():
    user = current_user()
    sql = "SELECT e.*, d.department_name FROM employees e JOIN departments d ON d.department_id=e.department_id"
    params = []
    if user and user["role"] == "Manager":
        sql += " WHERE e.department_id = ?"
        params.append(user["department_id"])
    sql += " ORDER BY e.first_name, e.last_name"
    return get_db().execute(sql, params).fetchall()


def get_record_or_404(record_id: int):
    rows = scoped_cert_query(include_archived=True, record_id=record_id)
    if not rows:
        abort(404)
    return rows[0]


@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("dashboard"))
    db = get_db()
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        job_role = request.form.get("job_role", "").strip()
        department_id = request.form.get("department_id", "").strip()
        if not all((first_name, last_name, email, username, password, confirm_password, department_id)):
            flash("Please complete all required fields.", "error")
        elif not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            flash("Enter a valid email address.", "error")
        elif len(password) < 10:
            flash("Your password must contain at least 10 characters.", "error")
        elif password != confirm_password:
            flash("The passwords do not match.", "error")
        elif not department_id.isdigit() or not db.execute("SELECT 1 FROM departments WHERE department_id=?", (int(department_id) if department_id.isdigit() else -1,)).fetchone():
            flash("Choose a valid department.", "error")
        elif db.execute("SELECT 1 FROM employees WHERE lower(email)=lower(?)", (email,)).fetchone():
            flash("An employee already uses this email. Contact the administrator.", "error")
        elif db.execute("SELECT 1 FROM user_accounts WHERE lower(username)=lower(?)", (username,)).fetchone():
            flash("That username is already in use. Choose another one.", "error")
        elif db.execute("SELECT 1 FROM registration_requests WHERE lower(email)=lower(?) AND status='Pending'", (email,)).fetchone():
            flash("A registration request for this email is already pending.", "error")
        elif db.execute("SELECT 1 FROM registration_requests WHERE lower(username)=lower(?) AND status='Pending'", (username,)).fetchone():
            flash("A registration request for this username is already pending.", "error")
        else:
            db.execute("INSERT INTO registration_requests(first_name,last_name,email,username,password_hash,job_role,department_id) VALUES (?,?,?,?,?,?,?)", (first_name, last_name, email, username, generate_password_hash(password), job_role, int(department_id)))
            db.commit()
            flash("Registration submitted. An administrator must approve your account before you can sign in.", "success")
            return redirect(url_for("login"))
    departments = db.execute("SELECT d.department_id, d.department_name, l.location_name FROM departments d JOIN locations l ON l.location_id=d.location_id ORDER BY d.department_name").fetchall()
    return render_template("register.html", departments=departments)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM user_accounts WHERE username = ? COLLATE NOCASE AND active=1", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session["user_id"] = row["user_id"]
            audit("USER_ACCOUNT", row["user_id"], "LOGIN", "Successful login")
            get_db().commit()
            dest = request.args.get("next", "")
            if not dest.startswith("/") or dest.startswith("//"):
                dest = url_for("dashboard")
            return redirect(dest)
        flash("Email or password is incorrect.", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    ensure_all_reminders()
    records = scoped_cert_query()
    statuses = [status_for(row["expiry_date"]) for row in records]
    summary = {
        "total": len(records),
        "valid": statuses.count("Valid"),
        "expiring": statuses.count("Expiring Soon"),
        "expired": statuses.count("Expired"),
    }
    employees = get_employees_for_user()
    upcoming = [dict(row, compliance_status=status_for(row["expiry_date"])) for row in records if status_for(row["expiry_date"]) == "Expiring Soon"]
    archived_count = db.execute(
        "SELECT COUNT(*) FROM employee_certifications ec JOIN employees e ON e.employee_id=ec.employee_id WHERE ec.is_archived=1" +
        (" AND e.department_id=?" if current_user()["role"] == "Manager" else ""),
        (current_user()["department_id"],) if current_user()["role"] == "Manager" else (),
    ).fetchone()[0]
    today = date.today().isoformat()
    reminders = [dict(row, reminder_state=("Due now" if row["scheduled_date"] <= today else "Scheduled")) for row in scoped_reminder_query("Pending")[:8]]
    return render_template("dashboard.html", summary=summary, records=records[:8], statuses=statuses[:8], upcoming=upcoming[:5], reminders=reminders, employees=employees, archived_count=archived_count)


@app.route("/certificates")
@login_required
def certificates():
    archived = request.args.get("archived") == "1"
    records = scoped_cert_query(include_archived=archived)
    if archived:
        records = [row for row in records if row["is_archived"] == 1]
    search = request.args.get("q", "").strip().lower()
    status_filter = request.args.get("status", "")
    department_filter = request.args.get("department", "")
    filtered = []
    for row in records:
        status = status_for(row["expiry_date"])
        haystack = " ".join([row["employee_code"], row["first_name"], row["last_name"], row["certification_title"], row["provider_name"], row["certificate_number"]]).lower()
        if search and search not in haystack:
            continue
        if status_filter and status != status_filter:
            continue
        if department_filter and str(row["department_id"]) != department_filter:
            continue
        filtered.append(dict(row, compliance_status=status))
    departments = get_db().execute("SELECT * FROM departments ORDER BY department_name").fetchall()
    if current_user()["role"] == "Manager":
        departments = [d for d in departments if d["department_id"] == current_user()["department_id"]]
    return render_template("certificates.html", records=filtered, departments=departments, search=search, status_filter=status_filter, department_filter=department_filter, archived=archived)


@app.route("/certificates/upload")
@login_required
@roles_required("Admin", "HR_Staff")
def upload_certificate():
    return render_template("upload.html")


@app.post("/certificates/scan")
@login_required
@roles_required("Admin", "HR_Staff")
def scan_certificate():
    uploaded = request.files.get("certificate_file")
    if not uploaded or not uploaded.filename:
        flash("Choose a PDF or image file first.", "error")
        return redirect(url_for("upload_certificate"))
    original = secure_filename(uploaded.filename)
    if not allowed_file(original):
        flash("Unsupported file type. Upload a PDF, PNG, JPG, TIFF, BMP, or WEBP file.", "error")
        return redirect(url_for("upload_certificate"))
    ext = Path(original).suffix.lower()
    scan_id = uuid.uuid4().hex
    path = STAGING_DIR / f"{scan_id}{ext}"
    uploaded.save(path)
    try:
        text = ocr_file(path)
        fields = extract_certificate_fields(text)
        fields = infer_expiry_when_not_printed(fields)
    except ValueError as exc:
        path.unlink(missing_ok=True)
        flash(str(exc), "error")
        return redirect(url_for("upload_certificate"))
    except Exception:
        path.unlink(missing_ok=True)
        app.logger.exception("Certificate scan failed")
        flash("The certificate could not be processed. Try another scan or enter the details manually.", "error")
        return redirect(url_for("upload_certificate"))
    payload = {"original_filename": original, "extension": ext, "fields": fields, "ocr_text": text}
    (STAGING_DIR / f"{scan_id}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return redirect(url_for("review_certificate", scan_id=scan_id))


def load_staged_scan(scan_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", scan_id or ""):
        abort(404)
    json_path = STAGING_DIR / f"{scan_id}.json"
    if not json_path.is_file():
        abort(404)
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        abort(404)


@app.route("/certificates/review/<scan_id>")
@login_required
@roles_required("Admin", "HR_Staff")
def review_certificate(scan_id):
    payload = load_staged_scan(scan_id)
    fields = dict(payload["fields"])
    fields["_ocr_text"] = payload.get("ocr_text", "")
    employee_rows = get_employees_for_user()
    detected_name = re.sub(r"\s+", " ", fields.get("employee_name", "")).strip().casefold()
    if detected_name:
        for employee in employee_rows:
            full_name = f"{employee['first_name']} {employee['last_name']}".strip().casefold()
            if full_name == detected_name:
                # Exact match only; the review form still asks the HR user to verify it.
                fields["employee_id"] = str(employee["employee_id"])
                break
    return render_certificate_form(fields=fields, scan_id=scan_id, original_filename=payload["original_filename"], employees=employee_rows, editing=None)


def render_certificate_form(fields=None, scan_id="", original_filename="", employees=None, editing=None, renew_from=None):
    if fields is None:
        fields = {}
    programs = get_db().execute(
        "SELECT cp.title, p.provider_name FROM certification_programs cp JOIN providers p ON p.provider_id=cp.provider_id ORDER BY cp.title"
    ).fetchall()
    return render_template(
        "certificate_form.html", fields=fields, scan_id=scan_id,
        original_filename=original_filename, employees=employees or get_employees_for_user(),
        programs=programs, editing=editing, renew_from=renew_from,
    )


@app.route("/certificates/new")
@login_required
@roles_required("Admin", "HR_Staff")
def new_certificate():
    fields = {
        "title": request.args.get("title", ""), "provider": request.args.get("provider", ""),
        "issue_date": "", "expiry_date": "", "certificate_number": "", "employee_id": "",
    }
    return render_certificate_form(fields=fields, employees=get_employees_for_user())


@app.post("/certificates/save")
@login_required
@roles_required("Admin", "HR_Staff")
def save_certificate():
    db = get_db()
    scan_id = request.form.get("scan_id", "").strip()
    record_id_raw = request.form.get("record_id", "").strip()
    renew_from_raw = request.form.get("renew_from_id", "").strip()
    employee_id = request.form.get("employee_id", "").strip()
    title = request.form.get("title", "").strip()
    provider_name = request.form.get("provider", "").strip()
    issue_date = request.form.get("issue_date", "").strip()
    expiry_date = request.form.get("expiry_date", "").strip()
    cert_no = request.form.get("certificate_number", "").strip()
    errors = []
    payload = None
    staged_file = None
    previous = None
    existing_record_id = int(record_id_raw) if record_id_raw.isdigit() else None

    if not employee_id.isdigit() or not db.execute("SELECT 1 FROM employees WHERE employee_id=?", (int(employee_id) if employee_id.isdigit() else -1,)).fetchone():
        errors.append("Choose a valid employee.")
    if not title:
        errors.append("Certification title is required.")
    if not provider_name:
        errors.append("Certification provider is required.")
    if renew_from_raw:
        if not renew_from_raw.isdigit() or existing_record_id:
            errors.append("The renewal reference is invalid. Start the renewal from the previous certificate record.")
        else:
            previous = get_record_or_404(int(renew_from_raw))
            if employee_id.isdigit() and int(employee_id) != previous["employee_id"]:
                errors.append("A renewal must remain linked to the same employee as the previous certificate.")
            if title.casefold() != previous["certification_title"].casefold() or provider_name.casefold() != previous["provider_name"].casefold():
                errors.append("A renewal must use the same certification title and provider as the previous record.")
    try:
        issue = date.fromisoformat(issue_date)
        expiry = date.fromisoformat(expiry_date)
        if expiry <= issue:
            errors.append("Expiry date must be after the issue date.")
    except ValueError:
        errors.append("Enter valid issue and expiry dates.")

    if cert_no and employee_id.isdigit():
        duplicate_params = [int(employee_id), cert_no]
        duplicate_sql = """SELECT record_id FROM employee_certifications
                          WHERE employee_id=? AND certificate_number=? COLLATE NOCASE
                          AND certificate_number<>'' AND is_archived=0"""
        if existing_record_id:
            duplicate_sql += " AND record_id<>?"
            duplicate_params.append(existing_record_id)
        duplicate = db.execute(duplicate_sql, duplicate_params).fetchone()
        if duplicate:
            errors.append(f"This credential ID already exists for the selected employee (record #{duplicate['record_id']}). Check for a duplicate upload.")

    if scan_id:
        payload = load_staged_scan(scan_id)
        staged_file = next((p for p in STAGING_DIR.glob(f"{scan_id}.*") if p.suffix.lower() != ".json"), None)
        if staged_file is None:
            errors.append("The uploaded certificate file is no longer available. Upload it again.")

    if errors:
        for err in errors:
            flash(err, "error")
        retained_fields = request.form.to_dict()
        if payload:
            retained_fields["_ocr_text"] = payload.get("ocr_text", "")
        if renew_from_raw.isdigit() and previous is None:
            previous = get_record_or_404(int(renew_from_raw))
        return render_certificate_form(
            fields=retained_fields, scan_id=scan_id,
            original_filename=payload.get("original_filename", "") if payload else "",
            employees=get_employees_for_user(), editing=existing_record_id,
            renew_from=previous,
        )

    employee_id_int = int(employee_id)
    provider_id = create_or_get_provider(provider_name)
    certification_id = create_or_get_program(title, provider_id)
    stored_filename = ""
    original_filename = ""
    ocr_text = ""
    if payload and staged_file:
        original_filename = payload.get("original_filename", "")
        ocr_text = payload.get("ocr_text", "")
        stored_key = f"{uuid.uuid4().hex}{staged_file.suffix.lower()}"
        try:
            stored_filename = persist_certificate_file(staged_file, stored_key)
        except ValueError as exc:
            flash(str(exc), "error")
            retained_fields = request.form.to_dict()
            retained_fields["_ocr_text"] = payload.get("ocr_text", "")
            return render_certificate_form(fields=retained_fields, scan_id=scan_id,
                                           original_filename=payload.get("original_filename", ""),
                                           employees=get_employees_for_user(), editing=existing_record_id,
                                           renew_from=previous)
        (STAGING_DIR / f"{scan_id}.json").unlink(missing_ok=True)

    date_changed = False
    if existing_record_id:
        record_id = existing_record_id
        old = get_record_or_404(record_id)
        if old["is_archived"]:
            flash("Archived records cannot be edited. Create a renewal record instead.", "error")
            return redirect(url_for("certificates", archived=1))
        date_changed = old["expiry_date"] != expiry_date or old["issue_date"] != issue_date
        db.execute(
            """UPDATE employee_certifications SET employee_id=?, certification_id=?, issue_date=?, expiry_date=?, certificate_number=?,
               original_filename=CASE WHEN ? != '' THEN ? ELSE original_filename END,
               stored_filename=CASE WHEN ? != '' THEN ? ELSE stored_filename END,
               ocr_text=CASE WHEN ? != '' THEN ? ELSE ocr_text END, updated_at=CURRENT_TIMESTAMP WHERE record_id=?""",
            (employee_id_int, certification_id, issue_date, expiry_date, cert_no, stored_filename, original_filename, stored_filename, stored_filename, ocr_text, ocr_text, record_id),
        )
        if date_changed:
            db.execute("DELETE FROM notification_reminders WHERE record_id=?", (record_id,))
        audit("EMPLOYEE_CERTIFICATION", record_id, "UPDATE", f"Updated certification: {title}")
        flash("Certificate record updated. Reminder dates were refreshed if the validity dates changed.", "success")
    else:
        supersedes_id = int(renew_from_raw) if renew_from_raw.isdigit() else None
        cur = db.execute(
            """INSERT INTO employee_certifications(employee_id, certification_id, issue_date, expiry_date, certificate_number, original_filename, stored_filename, ocr_text, supersedes_record_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (employee_id_int, certification_id, issue_date, expiry_date, cert_no, original_filename, stored_filename, ocr_text, supersedes_id),
        )
        record_id = cur.lastrowid
        audit("EMPLOYEE_CERTIFICATION", record_id, "INSERT", f"Created certification: {title}" + (f" as renewal of record #{supersedes_id}" if supersedes_id else ""))
        if supersedes_id:
            flash(f"Renewal saved as record #{record_id}. Previous record #{supersedes_id} was retained unchanged in history.", "success")
        else:
            flash("Certificate saved. Expiry status and in-app reminders were calculated automatically.", "success")

    db.commit()
    ensure_reminders_for_record(record_id, reset=True if (not existing_record_id or date_changed) else False)
    return redirect(url_for("certificates"))


@app.route("/certificates/<int:record_id>/edit")
@login_required
@roles_required("Admin", "HR_Staff")
def edit_certificate(record_id):
    record = get_record_or_404(record_id)
    if record["is_archived"]:
        flash("Archived records are read-only.", "error")
        return redirect(url_for("certificates", archived=1))
    fields = {
        "employee_id": str(record["employee_id"]), "title": record["certification_title"],
        "provider": record["provider_name"], "issue_date": record["issue_date"],
        "expiry_date": record["expiry_date"], "certificate_number": record["certificate_number"],
        "renew_from_id": "",
    }
    return render_certificate_form(fields=fields, employees=get_employees_for_user(), editing=record_id, original_filename=record["original_filename"])


@app.post("/certificates/<int:record_id>/archive")
@login_required
@roles_required("Admin", "HR_Staff")
def archive_certificate(record_id):
    record = get_record_or_404(record_id)
    db = get_db()
    db.execute("UPDATE employee_certifications SET is_archived=1, updated_at=CURRENT_TIMESTAMP WHERE record_id=?", (record_id,))
    db.execute("DELETE FROM notification_reminders WHERE record_id=? AND status='Pending'", (record_id,))
    audit("EMPLOYEE_CERTIFICATION", record_id, "ARCHIVE", f"Archived record {record_id}; historical data retained")
    db.commit()
    flash(f"Record #{record_id} archived; the historical record was retained.", "success")
    return redirect(url_for("certificates"))


@app.post("/certificates/<int:record_id>/restore")
@login_required
@roles_required("Admin", "HR_Staff")
def restore_certificate(record_id):
    record = get_record_or_404(record_id)
    db = get_db()
    db.execute("UPDATE employee_certifications SET is_archived=0, updated_at=CURRENT_TIMESTAMP WHERE record_id=?", (record_id,))
    audit("EMPLOYEE_CERTIFICATION", record_id, "RESTORE", "Restored archived certificate")
    db.commit()
    ensure_reminders_for_record(record_id, reset=True)
    flash("Certificate restored.", "success")
    return redirect(url_for("certificates", archived=1))


@app.route("/certificates/<int:record_id>/file")
@login_required
def certificate_file(record_id):
    record = get_record_or_404(record_id)
    if not record["stored_filename"]:
        abort(404)
    stored_name = record["stored_filename"]
    if stored_name.startswith("remote:"):
        data, content_type = fetch_certificate_file(stored_name)
        return send_file(BytesIO(data), mimetype=content_type, as_attachment=True,
                         download_name=record["original_filename"] or "certificate")
    path = UPLOAD_DIR / Path(stored_name).name
    if not path.exists():
        abort(404)
    return send_from_directory(UPLOAD_DIR, path.name, as_attachment=True, download_name=record["original_filename"] or path.name)


@app.route("/employees")
@login_required
def employees():
    search = request.args.get("q", "").strip()
    sql = """SELECT e.*, d.department_name, l.location_name,
           (SELECT COUNT(*) FROM employee_certifications ec WHERE ec.employee_id=e.employee_id AND ec.is_archived=0) AS certification_count
           FROM employees e JOIN departments d ON d.department_id=e.department_id JOIN locations l ON l.location_id=d.location_id"""
    clauses, params = [], []
    if current_user()["role"] == "Manager":
        clauses.append("e.department_id=?")
        params.append(current_user()["department_id"])
    if search:
        pattern = f"%{search}%"
        clauses.append("(e.employee_code LIKE ? OR e.first_name LIKE ? OR e.last_name LIKE ? OR e.email LIKE ? OR d.department_name LIKE ?)")
        params.extend([pattern] * 5)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY e.first_name, e.last_name"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("employees.html", employees=rows, search=search)


@app.route("/employees/new", methods=["GET", "POST"])
@login_required
@roles_required("Admin", "HR_Staff")
def new_employee():
    db = get_db()
    departments = db.execute("SELECT * FROM departments ORDER BY department_name").fetchall()
    if request.method == "POST":
        code = request.form.get("employee_code", "").strip().upper()
        first = request.form.get("first_name", "").strip()
        last = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        job = request.form.get("job_role", "").strip()
        dept_id = request.form.get("department_id", "").strip()
        errors = []
        if not all([code, first, last, email, dept_id]):
            errors.append("Employee code, name, email, and department are required.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors.append("Enter a valid email address.")
        if not dept_id.isdigit() or not db.execute("SELECT 1 FROM departments WHERE department_id=?", (int(dept_id) if dept_id.isdigit() else -1,)).fetchone():
            errors.append("Choose a valid department.")
        if db.execute("SELECT 1 FROM employees WHERE employee_code=? OR email=? COLLATE NOCASE", (code, email)).fetchone():
            errors.append("That employee code or email is already registered.")
        if errors:
            for err in errors:
                flash(err, "error")
        else:
            cur = db.execute("INSERT INTO employees(employee_code, first_name, last_name, email, job_role, department_id) VALUES (?, ?, ?, ?, ?, ?)", (code, first, last, email, job, int(dept_id)))
            audit("EMPLOYEE", cur.lastrowid, "INSERT", f"Added employee {code}")
            db.commit()
            flash("Employee added successfully.", "success")
            return redirect(url_for("employees"))
    return render_template("employee_form.html", departments=departments, fields=request.form if request.method == "POST" else {}, editing=None)


@app.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required("Admin", "HR_Staff")
def edit_employee(employee_id):
    db = get_db()
    employee = db.execute("SELECT * FROM employees WHERE employee_id=?", (employee_id,)).fetchone()
    if not employee:
        abort(404)
    departments = db.execute("SELECT * FROM departments ORDER BY department_name").fetchall()
    fields = dict(employee)
    if request.method == "POST":
        fields = request.form.to_dict()
        code = request.form.get("employee_code", "").strip().upper()
        first = request.form.get("first_name", "").strip()
        last = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        job = request.form.get("job_role", "").strip()
        dept_id = request.form.get("department_id", "").strip()
        errors = []
        if not all([code, first, last, email, dept_id]):
            errors.append("Employee code, name, email, and department are required.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors.append("Enter a valid email address.")
        if not dept_id.isdigit() or not db.execute("SELECT 1 FROM departments WHERE department_id=?", (int(dept_id) if dept_id.isdigit() else -1,)).fetchone():
            errors.append("Choose a valid department.")
        duplicate = db.execute("SELECT 1 FROM employees WHERE (employee_code=? OR email=? COLLATE NOCASE) AND employee_id<>?", (code, email, employee_id)).fetchone()
        if duplicate:
            errors.append("That employee code or email is already registered to another employee.")
        if errors:
            for err in errors:
                flash(err, "error")
        else:
            db.execute("UPDATE employees SET employee_code=?, first_name=?, last_name=?, email=?, job_role=?, department_id=? WHERE employee_id=?", (code, first, last, email, job, int(dept_id), employee_id))
            audit("EMPLOYEE", employee_id, "UPDATE", f"Updated employee {code}")
            db.commit()
            flash("Employee details updated.", "success")
            return redirect(url_for("employees"))
    return render_template("employee_form.html", departments=departments, fields=fields, editing=employee_id)


@app.post("/employees/<int:employee_id>/delete")
@login_required
@roles_required("Admin", "HR_Staff")
def delete_employee(employee_id):
    db = get_db()
    employee = db.execute("SELECT employee_code, first_name, last_name FROM employees WHERE employee_id=?", (employee_id,)).fetchone()
    if not employee:
        abort(404)
    if db.execute("SELECT 1 FROM employee_certifications WHERE employee_id=? LIMIT 1", (employee_id,)).fetchone():
        flash("This employee has certification history. Keep the employee record for audit continuity; do not delete it.", "error")
        return redirect(url_for("employees"))
    if db.execute("SELECT 1 FROM user_accounts WHERE employee_id=? LIMIT 1", (employee_id,)).fetchone():
        flash("This employee has a login account. Reassign or deactivate that account before deleting the employee.", "error")
        return redirect(url_for("employees"))
    audit("EMPLOYEE", employee_id, "DELETE", f"Deleted unused employee {employee['employee_code']}")
    db.execute("DELETE FROM employees WHERE employee_id=?", (employee_id,))
    db.commit()
    flash("Unused employee record deleted.", "success")
    return redirect(url_for("employees"))


@app.route("/employees/export.csv")
@login_required
@roles_required("Admin", "HR_Staff")
def export_employees():
    search = request.args.get("q", "").strip()
    sql = "SELECT e.employee_code, e.first_name, e.last_name, e.email, e.job_role, d.department_name, l.location_name FROM employees e JOIN departments d ON d.department_id=e.department_id JOIN locations l ON l.location_id=d.location_id"
    rows = get_db().execute(sql + " ORDER BY d.department_name, e.last_name, e.first_name").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Employee Code", "First Name", "Last Name", "Email", "Job Role", "Department", "Location"])
    for row in rows:
        values = [row["employee_code"], row["first_name"], row["last_name"], row["email"], row["job_role"], row["department_name"], row["location_name"]]
        if search and search.casefold() not in " ".join(str(v or "") for v in values).casefold():
            continue
        writer.writerow(values)
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=certtrack-employees.csv"
    return response


@app.route("/reminders")
@login_required
def reminders():
    ensure_all_reminders()
    status_filter = request.args.get("status", "Pending")
    if status_filter not in ("Pending", "Acknowledged", "Sent", "All"):
        status_filter = "Pending"
    rows = scoped_reminder_query("" if status_filter == "All" else status_filter)
    today = date.today()
    view_rows = []
    for row in rows:
        remaining = (date.fromisoformat(row["expiry_date"]) - today).days
        view_rows.append(dict(row, reminder_state=("Due now" if row["status"] == "Pending" and row["scheduled_date"] <= today.isoformat() else "Scheduled"), days_to_expiry=remaining))
    return render_template("reminders.html", reminders=view_rows, status_filter=status_filter)


@app.post("/reminders/<int:notification_id>/acknowledge")
@login_required
@roles_required("Admin", "HR_Staff")
def acknowledge_reminder(notification_id):
    db = get_db()
    row = db.execute(
        """SELECT nr.notification_id, nr.record_id, nr.status, ec.is_archived
           FROM notification_reminders nr JOIN employee_certifications ec ON ec.record_id=nr.record_id
           WHERE nr.notification_id=?""", (notification_id,)
    ).fetchone()
    if not row or row["is_archived"]:
        abort(404)
    if row["status"] == "Pending":
        db.execute("UPDATE notification_reminders SET status='Acknowledged' WHERE notification_id=?", (notification_id,))
        audit("NOTIFICATION_REMINDER", notification_id, "ACKNOWLEDGE", f"Acknowledged reminder for certification record #{row['record_id']}")
        db.commit()
        flash("Reminder acknowledged.", "success")
    else:
        flash("This reminder was already processed.", "info")
    return redirect(url_for("reminders"))


@app.route("/reminders/export.csv")
@login_required
def export_reminders():
    ensure_all_reminders()
    rows = scoped_reminder_query("")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Reminder ID", "Status", "Scheduled Date", "Employee Code", "Employee Name", "Department", "Certification", "Provider", "Expiry Date", "Days Until Expiry"])
    today = date.today()
    for row in rows:
        days_left = (date.fromisoformat(row["expiry_date"]) - today).days
        writer.writerow([row["notification_id"], row["status"], row["scheduled_date"], row["employee_code"], f"{row['first_name']} {row['last_name']}", row["department_name"], row["certification_title"], row["provider_name"], row["expiry_date"], days_left])
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=certtrack-reminders.csv"
    return response


@app.route("/certificates/<int:record_id>/renew")
@login_required
@roles_required("Admin", "HR_Staff")
def renew_certificate(record_id):
    previous = get_record_or_404(record_id)
    fields = {
        "employee_id": str(previous["employee_id"]),
        "title": previous["certification_title"],
        "provider": previous["provider_name"],
        "issue_date": date.today().isoformat(),
        "expiry_date": "",
        "certificate_number": "",
        "renew_from_id": str(record_id),
    }
    flash("Renewal creates a new record and keeps the previous certificate in history. Enter and verify the new expiry date.", "info")
    return render_certificate_form(fields=fields, employees=get_employees_for_user(), original_filename="", editing=None, renew_from=previous)


@app.route("/admin/manage", methods=["GET", "POST"])
@login_required
@roles_required("Admin")
def admin_manage():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            if action == "location_save":
                name = request.form.get("location_name", "").strip()
                region = request.form.get("region", "").strip()
                if not name:
                    raise ValueError("Location name is required.")
                key = request.form.get("location_id", "").strip()
                if key.isdigit():
                    db.execute("UPDATE locations SET location_name=?, region=? WHERE location_id=?", (name, region, int(key)))
                    audit("LOCATION", int(key), "UPDATE", f"Updated location {name}")
                else:
                    cur = db.execute("INSERT INTO locations(location_name, region) VALUES (?, ?)", (name, region))
                    audit("LOCATION", cur.lastrowid, "INSERT", f"Added location {name}")
                flash("Location saved.", "success")

            elif action == "location_delete":
                key = int(request.form.get("location_id", "0"))
                row = db.execute("SELECT location_name FROM locations WHERE location_id=?", (key,)).fetchone()
                if not row:
                    raise ValueError("Location not found.")
                if db.execute("SELECT 1 FROM departments WHERE location_id=?", (key,)).fetchone():
                    raise ValueError("This location is in use by a department. Move those departments first.")
                db.execute("DELETE FROM locations WHERE location_id=?", (key,))
                audit("LOCATION", key, "DELETE", f"Deleted unused location {row['location_name']}")
                flash("Unused location deleted.", "success")

            elif action == "department_save":
                name = request.form.get("department_name", "").strip()
                location_id = request.form.get("location_id", "").strip()
                key = request.form.get("department_id", "").strip()
                if not name or not location_id.isdigit() or not db.execute("SELECT 1 FROM locations WHERE location_id=?", (int(location_id) if location_id.isdigit() else -1,)).fetchone():
                    raise ValueError("Department name and a valid location are required.")
                if key.isdigit():
                    db.execute("UPDATE departments SET department_name=?, location_id=? WHERE department_id=?", (name, int(location_id), int(key)))
                    audit("DEPARTMENT", int(key), "UPDATE", f"Updated department {name}")
                else:
                    cur = db.execute("INSERT INTO departments(department_name, location_id) VALUES (?, ?)", (name, int(location_id)))
                    audit("DEPARTMENT", cur.lastrowid, "INSERT", f"Added department {name}")
                flash("Department saved.", "success")

            elif action == "department_delete":
                key = int(request.form.get("department_id", "0"))
                row = db.execute("SELECT department_name FROM departments WHERE department_id=?", (key,)).fetchone()
                if not row:
                    raise ValueError("Department not found.")
                if db.execute("SELECT 1 FROM employees WHERE department_id=?", (key,)).fetchone():
                    raise ValueError("This department is in use by employees. Reassign them first.")
                db.execute("DELETE FROM departments WHERE department_id=?", (key,))
                audit("DEPARTMENT", key, "DELETE", f"Deleted unused department {row['department_name']}")
                flash("Unused department deleted.", "success")

            elif action == "provider_save":
                name = request.form.get("provider_name", "").strip()
                email = request.form.get("contact_email", "").strip()
                key = request.form.get("provider_id", "").strip()
                if not name:
                    raise ValueError("Provider name is required.")
                if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                    raise ValueError("Enter a valid provider contact email or leave it blank.")
                if key.isdigit():
                    db.execute("UPDATE providers SET provider_name=?, contact_email=? WHERE provider_id=?", (name, email, int(key)))
                    audit("PROVIDER", int(key), "UPDATE", f"Updated provider {name}")
                else:
                    cur = db.execute("INSERT INTO providers(provider_name, contact_email) VALUES (?, ?)", (name, email))
                    audit("PROVIDER", cur.lastrowid, "INSERT", f"Added provider {name}")
                flash("Provider saved.", "success")

            elif action == "provider_delete":
                key = int(request.form.get("provider_id", "0"))
                row = db.execute("SELECT provider_name FROM providers WHERE provider_id=?", (key,)).fetchone()
                if not row:
                    raise ValueError("Provider not found.")
                if db.execute("SELECT 1 FROM certification_programs WHERE provider_id=?", (key,)).fetchone():
                    raise ValueError("This provider has certification programs. Move or remove them first.")
                db.execute("DELETE FROM providers WHERE provider_id=?", (key,))
                audit("PROVIDER", key, "DELETE", f"Deleted unused provider {row['provider_name']}")
                flash("Unused provider deleted.", "success")

            elif action == "program_save":
                title = request.form.get("title", "").strip()
                description = request.form.get("description", "").strip()
                provider_id = request.form.get("provider_id", "").strip()
                validity_raw = request.form.get("validity_months", "0").strip()
                key = request.form.get("certification_id", "").strip()
                if not title or not provider_id.isdigit() or not db.execute("SELECT 1 FROM providers WHERE provider_id=?", (int(provider_id) if provider_id.isdigit() else -1,)).fetchone():
                    raise ValueError("Certification title and a valid provider are required.")
                try:
                    validity = int(validity_raw)
                except ValueError:
                    raise ValueError("Validity must be a whole number of months; use 0 for no standard expiry.")
                if validity < 0 or validity > 600:
                    raise ValueError("Validity must be between 0 and 600 months.")
                if key.isdigit():
                    db.execute("UPDATE certification_programs SET title=?, description=?, validity_months=?, provider_id=? WHERE certification_id=?", (title, description, validity, int(provider_id), int(key)))
                    audit("CERTIFICATION_PROGRAM", int(key), "UPDATE", f"Updated certification program {title}")
                else:
                    cur = db.execute("INSERT INTO certification_programs(title, description, validity_months, provider_id) VALUES (?, ?, ?, ?)", (title, description, validity, int(provider_id)))
                    audit("CERTIFICATION_PROGRAM", cur.lastrowid, "INSERT", f"Added certification program {title}")
                flash("Certification program saved.", "success")

            elif action == "program_delete":
                key = int(request.form.get("certification_id", "0"))
                row = db.execute("SELECT title FROM certification_programs WHERE certification_id=?", (key,)).fetchone()
                if not row:
                    raise ValueError("Certification program not found.")
                if db.execute("SELECT 1 FROM employee_certifications WHERE certification_id=?", (key,)).fetchone():
                    raise ValueError("This program is referenced by certificate history and cannot be deleted.")
                db.execute("DELETE FROM certification_programs WHERE certification_id=?", (key,))
                audit("CERTIFICATION_PROGRAM", key, "DELETE", f"Deleted unused certification program {row['title']}")
                flash("Unused certification program deleted.", "success")

            elif action == "registration_approve":
                request_id = request.form.get("request_id", "").strip()
                role = request.form.get("role", "")
                if not request_id.isdigit() or role not in ("HR_Staff", "Manager"):
                    raise ValueError("Choose a valid registration request and role.")
                req = db.execute("SELECT * FROM registration_requests WHERE request_id=? AND status='Pending'", (int(request_id),)).fetchone()
                if not req:
                    raise ValueError("This registration request is no longer pending.")
                if db.execute("SELECT 1 FROM employees WHERE lower(email)=lower(?)", (req["email"],)).fetchone():
                    raise ValueError("An employee with this email already exists. Resolve the duplicate before approval.")
                if db.execute("SELECT 1 FROM user_accounts WHERE lower(username)=lower(?)", (req["username"],)).fetchone():
                    raise ValueError("This username is already in use. Ask the applicant to register again with another username.")
                employee_code = "REG-" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2).upper()
                cur = db.execute("INSERT INTO employees(employee_code, first_name, last_name, email, job_role, department_id) VALUES (?,?,?,?,?,?)", (employee_code, req["first_name"], req["last_name"], req["email"], req["job_role"] or role.replace("_", " "), req["department_id"]))
                employee_id = cur.lastrowid
                account = db.execute("INSERT INTO user_accounts(employee_id, username, password_hash, role) VALUES (?,?,?,?)", (employee_id, req["username"], req["password_hash"], role))
                db.execute("UPDATE registration_requests SET status='Approved', reviewed_at=CURRENT_TIMESTAMP, reviewed_by=?, assigned_role=? WHERE request_id=?", (session.get("user_id"), role, int(request_id)))
                audit("REGISTRATION_REQUEST", int(request_id), "APPROVED", f"Approved registration for {req['email']} as {role}; account #{account.lastrowid}")
                flash(f"Registration approved. {req['first_name']} {req['last_name']} can now sign in as {role.replace('_', ' ')}.", "success")

            elif action == "registration_reject":
                request_id = request.form.get("request_id", "").strip()
                req = db.execute("SELECT request_id, email FROM registration_requests WHERE request_id=? AND status='Pending'", (int(request_id) if request_id.isdigit() else -1,)).fetchone()
                if not req:
                    raise ValueError("This registration request is no longer pending.")
                db.execute("UPDATE registration_requests SET status='Rejected', reviewed_at=CURRENT_TIMESTAMP, reviewed_by=? WHERE request_id=?", (session.get("user_id"), int(request_id)))
                audit("REGISTRATION_REQUEST", int(request_id), "REJECTED", f"Rejected registration request for {req['email']}")
                flash("Registration request rejected.", "success")

            elif action == "account_create":
                employee_id = request.form.get("employee_id", "").strip()
                username = request.form.get("username", "").strip()
                password = request.form.get("password", "")
                role = request.form.get("role", "")
                if not employee_id.isdigit() or not db.execute("SELECT 1 FROM employees WHERE employee_id=?", (int(employee_id) if employee_id.isdigit() else -1,)).fetchone():
                    raise ValueError("Choose a valid employee for the account.")
                if not username:
                    raise ValueError("A username is required.")
                if len(password) < 10:
                    raise ValueError("Initial passwords must contain at least 10 characters.")
                if role not in ("Admin", "HR_Staff", "Manager"):
                    raise ValueError("Choose a valid account role.")
                cur = db.execute("INSERT INTO user_accounts(employee_id, username, password_hash, role) VALUES (?, ?, ?, ?)", (int(employee_id), username, generate_password_hash(password), role))
                audit("USER_ACCOUNT", cur.lastrowid, "INSERT", f"Created {role} account for employee #{employee_id}")
                flash("User account created. Share the initial password securely.", "success")

            elif action == "account_update":
                key = int(request.form.get("user_id", "0"))
                username = request.form.get("username", "").strip()
                role = request.form.get("role", "")
                active = 1 if request.form.get("active") == "1" else 0
                existing = db.execute("SELECT user_id FROM user_accounts WHERE user_id=?", (key,)).fetchone()
                if not existing or not username or role not in ("Admin", "HR_Staff", "Manager"):
                    raise ValueError("Choose a valid account, username, and role.")
                if key == session.get("user_id") and (not active or role != "Admin"):
                    raise ValueError("You cannot deactivate or remove administrator access from your own account.")
                db.execute("UPDATE user_accounts SET username=?, role=?, active=? WHERE user_id=?", (username, role, active, key))
                audit("USER_ACCOUNT", key, "UPDATE", f"Updated account username/role/active status: {role}, {'active' if active else 'inactive'}")
                flash("User account updated.", "success")

            elif action == "account_password":
                key = int(request.form.get("user_id", "0"))
                password = request.form.get("new_password", "")
                if not db.execute("SELECT 1 FROM user_accounts WHERE user_id=?", (key,)).fetchone():
                    raise ValueError("Account not found.")
                if len(password) < 10:
                    raise ValueError("New passwords must contain at least 10 characters.")
                db.execute("UPDATE user_accounts SET password_hash=? WHERE user_id=?", (generate_password_hash(password), key))
                audit("USER_ACCOUNT", key, "PASSWORD_RESET", "Administrator reset account password")
                flash("Password reset saved.", "success")
            else:
                raise ValueError("Unknown administration action.")
            db.commit()
        except ValueError as exc:
            db.rollback()
            flash(str(exc), "error")
        except ((sqlite3.IntegrityError, psycopg.IntegrityError) if psycopg is not None else sqlite3.IntegrityError):
            db.rollback()
            flash("Could not save this change: a unique name, email, username, employee account, or required relationship may already exist.", "error")
        return redirect(url_for("admin_manage"))

    locations = db.execute("SELECT * FROM locations ORDER BY location_name").fetchall()
    departments = db.execute("SELECT d.*, l.location_name FROM departments d JOIN locations l ON l.location_id=d.location_id ORDER BY d.department_name").fetchall()
    providers = db.execute("SELECT * FROM providers ORDER BY provider_name").fetchall()
    programs = db.execute("SELECT cp.*, p.provider_name, (SELECT COUNT(*) FROM employee_certifications ec WHERE ec.certification_id=cp.certification_id) AS usage_count FROM certification_programs cp JOIN providers p ON p.provider_id=cp.provider_id ORDER BY cp.title").fetchall()
    accounts = db.execute("SELECT u.*, e.employee_code, e.first_name, e.last_name, e.email FROM user_accounts u JOIN employees e ON e.employee_id=u.employee_id ORDER BY u.user_id").fetchall()
    available_employees = db.execute("SELECT e.employee_id, e.employee_code, e.first_name, e.last_name, e.email FROM employees e LEFT JOIN user_accounts u ON u.employee_id=e.employee_id WHERE u.user_id IS NULL ORDER BY e.first_name, e.last_name").fetchall()
    pending_registrations = db.execute("SELECT r.*, d.department_name, l.location_name FROM registration_requests r JOIN departments d ON d.department_id=r.department_id JOIN locations l ON l.location_id=d.location_id WHERE r.status='Pending' ORDER BY r.requested_at").fetchall()
    return render_template("admin_manage.html", locations=locations, departments=departments, providers=providers, programs=programs, accounts=accounts, available_employees=available_employees, pending_registrations=pending_registrations)


@app.route("/reports")
@login_required
def reports():
    rows = scoped_cert_query()
    summary = {"total": len(rows), "valid": 0, "expiring": 0, "expired": 0}
    dept_data = {}
    for row in rows:
        status = status_for(row["expiry_date"])
        key = {"Valid": "valid", "Expiring Soon": "expiring", "Expired": "expired"}.get(status)
        if key:
            summary[key] += 1
        dept = row["department_name"]
        dept_data.setdefault(dept, {"department_name": dept, "total": 0, "valid": 0, "expiring": 0, "expired": 0})
        dept_data[dept]["total"] += 1
        if key:
            dept_data[dept][key] += 1
    report_rows = [dict(r, compliance_status=status_for(r["expiry_date"])) for r in rows]
    return render_template("reports.html", summary=summary, departments=list(dept_data.values()), records=report_rows)


@app.route("/reports/export.csv")
@login_required
def export_report():
    rows = scoped_cert_query()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Record ID", "Employee Code", "Employee Name", "Department", "Certification", "Provider", "Issue Date", "Expiry Date", "Status", "Certificate Number", "Archived"])
    for r in rows:
        writer.writerow([r["record_id"], r["employee_code"], f'{r["first_name"]} {r["last_name"]}', r["department_name"], r["certification_title"], r["provider_name"], r["issue_date"], r["expiry_date"], status_for(r["expiry_date"]), r["certificate_number"], bool(r["is_archived"])])
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = "attachment; filename=certtrack-compliance-report.csv"
    return response


@app.route("/audit-log")
@login_required
@roles_required("Admin")
def audit_log():
    rows = get_db().execute(
        """SELECT a.*, u.username FROM system_audit_log a LEFT JOIN user_accounts u ON u.user_id=a.performed_by
           ORDER BY a.action_timestamp DESC, a.audit_id DESC LIMIT 200"""
    ).fetchall()
    return render_template("audit_log.html", logs=rows)


@app.get("/healthz")
def healthz():
    """Minimal liveness check for Render and container orchestration."""
    try:
        get_db().execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "postgres" if USE_POSTGRES else "sqlite"}, 200
    except Exception:
        app.logger.exception("Health check failed")
        return {"status": "error"}, 503


@app.errorhandler(400)
def bad_request(error):
    message = getattr(error, "description", "The request could not be accepted.")
    return render_template("error.html", code=400, message=message), 400


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="You do not have permission to access this page."), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="The page or record could not be found."), 404


@app.errorhandler(413)
def too_large(_error):
    flash("File is too large. The upload limit is 10 MB.", "error")
    return redirect(url_for("upload_certificate"))


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")))

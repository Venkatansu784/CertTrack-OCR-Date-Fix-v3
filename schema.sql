-- CertTrack relational schema
-- This mirrors the SQLite schema used by app.py. Execute with SQLite with foreign keys enabled.
-- app.py also runs backward-compatible migrations and creates performance indexes.


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

CREATE INDEX IF NOT EXISTS idx_employee_cert_expiry ON employee_certifications(expiry_date, is_archived);
CREATE INDEX IF NOT EXISTS idx_employee_cert_employee ON employee_certifications(employee_id, is_archived);
CREATE INDEX IF NOT EXISTS idx_reminder_status_date ON notification_reminders(status, scheduled_date);

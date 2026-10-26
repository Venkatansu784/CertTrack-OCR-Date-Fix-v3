-- CertTrack schema for Supabase PostgreSQL.
-- The Flask app also creates these tables automatically at startup.
-- Run as the Supabase database owner. Keep employee files in a private Storage bucket.
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS locations (
    location_id SERIAL PRIMARY KEY,
    location_name TEXT NOT NULL UNIQUE,
    region TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS departments (
    department_id SERIAL PRIMARY KEY,
    department_name TEXT NOT NULL UNIQUE,
    location_id INTEGER NOT NULL REFERENCES locations(location_id)
);
CREATE TABLE IF NOT EXISTS employees (
    employee_id SERIAL PRIMARY KEY,
    employee_code TEXT NOT NULL UNIQUE,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email CITEXT NOT NULL UNIQUE,
    job_role TEXT NOT NULL DEFAULT '',
    department_id INTEGER NOT NULL REFERENCES departments(department_id),
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)
);
CREATE TABLE IF NOT EXISTS providers (
    provider_id SERIAL PRIMARY KEY,
    provider_name CITEXT NOT NULL UNIQUE,
    contact_email TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS certification_programs (
    certification_id SERIAL PRIMARY KEY,
    title CITEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    validity_months INTEGER NOT NULL DEFAULT 12,
    provider_id INTEGER NOT NULL REFERENCES providers(provider_id),
    UNIQUE(title, provider_id)
);
CREATE TABLE IF NOT EXISTS user_accounts (
    user_id SERIAL PRIMARY KEY,
    employee_id INTEGER NOT NULL UNIQUE REFERENCES employees(employee_id),
    username CITEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('Admin', 'HR_Staff', 'Manager')),
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS registration_requests (
    request_id BIGSERIAL PRIMARY KEY,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT NOT NULL,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    job_role TEXT NOT NULL DEFAULT '',
    department_id INTEGER NOT NULL REFERENCES departments(department_id),
    status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Approved', 'Rejected')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMPTZ,
    reviewed_by INTEGER REFERENCES user_accounts(user_id),
    assigned_role TEXT CHECK(assigned_role IN ('HR_Staff', 'Manager'))
);
CREATE TABLE IF NOT EXISTS employee_certifications (
    record_id SERIAL PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(employee_id),
    certification_id INTEGER NOT NULL REFERENCES certification_programs(certification_id),
    issue_date TEXT NOT NULL,
    expiry_date TEXT NOT NULL,
    certificate_number TEXT NOT NULL DEFAULT '',
    original_filename TEXT NOT NULL DEFAULT '',
    stored_filename TEXT NOT NULL DEFAULT '',
    ocr_text TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    is_archived INTEGER NOT NULL DEFAULT 0,
    supersedes_record_id INTEGER REFERENCES employee_certifications(record_id),
    CHECK(expiry_date > issue_date)
);
CREATE TABLE IF NOT EXISTS notification_reminders (
    notification_id SERIAL PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES employee_certifications(record_id),
    scheduled_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'Sent', 'Acknowledged')),
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    UNIQUE(record_id, scheduled_date)
);
CREATE TABLE IF NOT EXISTS system_audit_log (
    audit_id SERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    action_type TEXT NOT NULL,
    action_timestamp TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text),
    performed_by INTEGER REFERENCES user_accounts(user_id),
    details TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_employee_cert_expiry ON employee_certifications(expiry_date, is_archived);
CREATE INDEX IF NOT EXISTS idx_employee_cert_employee ON employee_certifications(employee_id, is_archived);
CREATE INDEX IF NOT EXISTS idx_reminder_status_date ON notification_reminders(status, scheduled_date);

-- Deliberately no public client policies: all business access goes through the Flask server.
ALTER TABLE locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE departments ENABLE ROW LEVEL SECURITY;
ALTER TABLE employees ENABLE ROW LEVEL SECURITY;
ALTER TABLE providers ENABLE ROW LEVEL SECURITY;
ALTER TABLE certification_programs ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE employee_certifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_reminders ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_audit_log ENABLE ROW LEVEL SECURITY;

ALTER TABLE registration_requests ENABLE ROW LEVEL SECURITY;

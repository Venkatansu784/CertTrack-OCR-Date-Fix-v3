"""Send due CertTrack reminders to active Admin/HR users when SMTP is configured.

Run once per day with the project's virtual-environment Python. No email is sent
unless SMTP_HOST, SMTP_FROM and at least one HR/Admin email address are available.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage

from app import app, ensure_all_reminders, get_db


def main() -> int:
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_from = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USERNAME", "")).strip()
    if not smtp_host or not smtp_from:
        print("Email reminders are not configured. Set SMTP_HOST and SMTP_FROM; no mail was sent.")
        return 2

    try:
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        print("SMTP_PORT must be a whole number, such as 587 or 465.")
        return 2

    with app.app_context():
        ensure_all_reminders()
        db = get_db()
        recipients = [row["email"] for row in db.execute(
            """SELECT DISTINCT e.email FROM user_accounts u
               JOIN employees e ON e.employee_id = u.employee_id
               WHERE u.active = 1 AND u.role IN ('Admin', 'HR_Staff')
               AND e.email LIKE '%@%.%' ORDER BY e.email"""
        ).fetchall()]
        if not recipients:
            print("No active Admin/HR recipient email addresses are configured. No mail was sent.")
            return 2

        today = date.today()
        due = db.execute(
            """SELECT nr.notification_id, nr.record_id, nr.scheduled_date,
                      ec.expiry_date, e.employee_code, e.first_name, e.last_name,
                      e.email AS employee_email, d.department_name,
                      cp.title AS certification_title, p.provider_name
               FROM notification_reminders nr
               JOIN employee_certifications ec ON ec.record_id = nr.record_id
               JOIN employees e ON e.employee_id = ec.employee_id
               JOIN departments d ON d.department_id = e.department_id
               JOIN certification_programs cp ON cp.certification_id = ec.certification_id
               JOIN providers p ON p.provider_id = cp.provider_id
               WHERE nr.status = 'Pending' AND nr.scheduled_date <= ?
                 AND ec.is_archived = 0 AND ec.expiry_date >= ?
               ORDER BY nr.scheduled_date, ec.expiry_date""",
            (today.isoformat(), today.isoformat()),
        ).fetchall()

        if not due:
            print("No due reminders. Nothing to send.")
            return 0

        username = os.environ.get("SMTP_USERNAME", "").strip()
        password = os.environ.get("SMTP_PASSWORD", "")
        use_tls = os.environ.get("SMTP_USE_TLS", "1").strip().lower() not in ("0", "false", "no")
        sent_ids: list[int] = []
        tls_context = ssl.create_default_context()

        try:
            if smtp_port == 465:
                server_context = smtplib.SMTP_SSL(smtp_host, smtp_port, context=tls_context, timeout=30)
            else:
                server_context = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            with server_context as smtp:
                smtp.ehlo()
                if smtp_port != 465 and use_tls:
                    smtp.starttls(context=tls_context)
                    smtp.ehlo()
                if username:
                    smtp.login(username, password)
                for reminder in due:
                    days_left = (date.fromisoformat(reminder["expiry_date"]) - today).days
                    deadline = date.fromisoformat(reminder["expiry_date"]).strftime("%d %B %Y")
                    subject = f"CertTrack renewal alert: {reminder['certification_title']} expires {deadline}"
                    body = (
                        "CertTrack certification renewal reminder\n\n"
                        f"Employee: {reminder['first_name']} {reminder['last_name']} ({reminder['employee_code']})\n"
                        f"Department: {reminder['department_name']}\n"
                        f"Certification: {reminder['certification_title']}\n"
                        f"Provider: {reminder['provider_name']}\n"
                        f"Expiry date: {deadline}\n"
                        f"Days remaining: {days_left}\n\n"
                        "Please verify the certificate and arrange renewal if required.\n"
                        "This is an automated notice from CertTrack."
                    )
                    for recipient in recipients:
                        message = EmailMessage()
                        message["Subject"] = subject
                        message["From"] = smtp_from
                        message["To"] = recipient
                        message.set_content(body)
                        smtp.send_message(message)
                    sent_ids.append(reminder["notification_id"])
        except (OSError, smtplib.SMTPException) as exc:
            print(f"Mail delivery failed; unsent reminders remain Pending. Details: {exc}")
            return 1

        for notification_id in sent_ids:
            db.execute("UPDATE notification_reminders SET status='Sent' WHERE notification_id=? AND status='Pending'", (notification_id,))
            db.execute(
                "INSERT INTO system_audit_log(entity_type, entity_id, action_type, details, performed_by) VALUES ('NOTIFICATION_REMINDER', ?, 'SENT', 'Reminder email delivered to configured Admin/HR recipients', NULL)",
                (notification_id,),
            )
        db.commit()
        print(f"Sent {len(sent_ids)} reminder(s) to {len(recipients)} Admin/HR recipient(s).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

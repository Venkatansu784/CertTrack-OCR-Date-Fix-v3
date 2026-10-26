"""Regression tests for reminder milestone creation and idempotency.

Run from the project folder with:
  .\\.venv\\Scripts\\python.exe -m unittest discover -s tests
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import app as certtrack


class ReminderMilestoneTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = certtrack.DATABASE
        certtrack.DATABASE = Path(self.temp_dir.name) / "test_certtrack.sqlite3"
        certtrack.init_db()
        self.context = certtrack.app.app_context()
        self.context.push()
        self.db = certtrack.get_db()
        self.expiry = date.today() + timedelta(days=18)
        self.db.execute(
            "UPDATE employee_certifications SET expiry_date=? WHERE record_id=1",
            (self.expiry.isoformat(),),
        )
        self.db.execute("DELETE FROM notification_reminders WHERE record_id=1")
        self.db.commit()

    def tearDown(self):
        self.context.pop()
        certtrack.DATABASE = self.original_database
        self.temp_dir.cleanup()

    def test_late_entry_keeps_original_milestone_date(self):
        certtrack.ensure_reminders_for_record(1, reset=True)
        rows = self.db.execute(
            "SELECT scheduled_date FROM notification_reminders WHERE record_id=1 ORDER BY scheduled_date"
        ).fetchall()
        actual = {row["scheduled_date"] for row in rows}
        expected = {
            (self.expiry - timedelta(days=30)).isoformat(),
            (self.expiry - timedelta(days=7)).isoformat(),
            (self.expiry - timedelta(days=1)).isoformat(),
        }
        self.assertEqual(actual, expected)
        self.assertEqual(len(rows), 3)
        self.assertIn((date.today() - timedelta(days=12)).isoformat(), actual)

    def test_repeated_page_checks_do_not_create_daily_due_reminders(self):
        certtrack.ensure_reminders_for_record(1, reset=True)
        first_rows = self.db.execute(
            "SELECT notification_id, scheduled_date FROM notification_reminders WHERE record_id=1 ORDER BY scheduled_date"
        ).fetchall()

        class Tomorrow(date):
            @classmethod
            def today(cls):
                return date.today() + timedelta(days=1)

        # Simulate the next day to reproduce the old bug where an overdue
        # milestone was rewritten to today's date and re-created every day.
        with patch.object(certtrack, "date", Tomorrow):
            certtrack.ensure_reminders_for_record(1)

        second_rows = self.db.execute(
            "SELECT notification_id, scheduled_date FROM notification_reminders WHERE record_id=1 ORDER BY scheduled_date"
        ).fetchall()
        self.assertEqual(len(second_rows), 3)
        self.assertEqual(
            [(row["notification_id"], row["scheduled_date"]) for row in first_rows],
            [(row["notification_id"], row["scheduled_date"]) for row in second_rows],
        )


if __name__ == "__main__":
    unittest.main()

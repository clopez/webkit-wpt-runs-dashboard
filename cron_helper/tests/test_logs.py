import datetime
import logging
import pathlib
import re
import tempfile
import unittest

import logs

TODAY = datetime.date(2026, 9, 29)


def line(date, message):
    return f"[{date.isoformat()}|12:00:00|UTC] {message}\n"


class FormatterTest(unittest.TestCase):
    def format(self, level, message):
        record = logging.LogRecord(logs.LOGGER_NAME, level, __file__, 1, message, None, None)
        return logs.DashboardFormatter().format(record)

    def test_lines_start_with_the_filter_test_logs_timestamp(self):
        self.assertRegex(self.format(logging.INFO, "Starting update"), r"^\[\d{4}-\d{2}-\d{2}\|\d{2}:\d{2}:\d{2}\|[^\]]+\] Starting update$")

    def test_errors_and_warnings_have_a_prefix_and_stay_on_one_line(self):
        self.assertRegex(self.format(logging.ERROR, "it broke\nbadly"), r"\] ERROR: it broke \| badly$")
        self.assertRegex(self.format(logging.WARNING, "retried"), r"\] WARNING: retried$")


class RotateTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)
        self.log = self.root / "data" / "cron_helper.log.txt"
        self.log.parent.mkdir()
        self.archives = self.root / "logs"

    def archive_lines(self):
        return {path.name: path.read_text() for path in sorted(self.archives.glob("*.txt"))}

    def test_lines_older_than_30_days_move_to_the_archives(self):
        old = TODAY - datetime.timedelta(days=31)
        self.log.write_text(line(old, "old") + "continuation of the old line\n" + line(TODAY, "new"))
        logs.rotate(self.log, self.archives, TODAY)
        self.assertEqual(self.log.read_text(), line(TODAY, "new"))
        archived = "".join(self.archive_lines().values())
        self.assertEqual(archived, line(old, "old") + "continuation of the old line\n")

    def test_archives_keep_at_most_90_days(self):
        very_old = TODAY - datetime.timedelta(days=95)
        old = TODAY - datetime.timedelta(days=45)
        self.log.write_text(line(very_old, "very old") + line(old, "old") + line(TODAY, "new"))
        logs.rotate(self.log, self.archives, TODAY)
        archived = "".join(self.archive_lines().values())
        self.assertNotIn("very old", archived)
        self.assertIn("] old", archived)

    def test_every_archive_holds_one_block_of_30_days(self):
        dates = [TODAY - datetime.timedelta(days=days) for days in (40, 50, 70, 80)]
        self.log.write_text("".join(line(date, date.isoformat()) for date in dates))
        logs.rotate(self.log, self.archives, TODAY)
        for name, text in self.archive_lines().items():
            start = datetime.date.fromisoformat(re.search(r"(\d{4}-\d{2}-\d{2})\.txt$", name)[1])
            for found in re.findall(r"^\[(\d{4}-\d{2}-\d{2})", text, re.M):
                self.assertTrue(start <= datetime.date.fromisoformat(found) < start + datetime.timedelta(days=30))

    def test_old_archives_are_pruned_even_without_a_log(self):
        self.archives.mkdir()
        expired = self.archives / "cron_helper.log.2026-01-01.txt"
        expired.write_text(line(datetime.date(2026, 1, 5), "expired"))
        logs.rotate(self.log, self.archives, TODAY)
        self.assertFalse(expired.exists())

    def test_a_line_cut_in_the_middle_of_a_character_does_not_stop_the_rotation(self):
        self.log.write_bytes(line(TODAY, "cut").encode() + b"\xe2\x82")
        logs.rotate(self.log, self.archives, TODAY)
        self.assertIn("cut", self.log.read_text(errors="replace"))

    def test_the_log_keeps_exactly_30_days(self):
        self.log.write_text("".join(line(TODAY - datetime.timedelta(days=days), "x") for days in range(35)))
        logs.rotate(self.log, self.archives, TODAY)
        self.assertEqual(len(self.log.read_text().splitlines()), 30)

    def test_nothing_changes_when_every_line_is_recent(self):
        self.log.write_text(line(TODAY, "new"))
        logs.rotate(self.log, self.archives, TODAY)
        self.assertEqual(self.log.read_text(), line(TODAY, "new"))
        self.assertFalse(self.archives.exists())


if __name__ == "__main__":
    unittest.main()

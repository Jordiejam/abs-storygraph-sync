import unittest

from history import build_history_preview


class HistoryPreviewTests(unittest.TestCase):
    def test_groups_sessions_into_daily_monotonic_checkpoints(self):
        result = build_history_preview([
            {"date": "2026-09-18", "startTime": 0, "currentTime": 1800, "timeListening": 1750},
            {"date": "2026-09-18", "startTime": 1800, "currentTime": 3600, "timeListening": 1700},
            {"date": "2026-09-19", "startTime": 3500, "currentTime": 5400, "timeListening": 1800},
        ], duration_minutes=180)

        self.assertEqual(3, result["summary"]["session_count"])
        self.assertEqual(2, result["summary"]["day_count"])
        self.assertEqual(60.0, result["days"][0]["end_position_minutes"])
        self.assertEqual(33.3, result["days"][0]["progress_percent"])
        self.assertEqual(90.0, result["days"][1]["end_position_minutes"])
        self.assertEqual("high", result["summary"]["confidence"])

    def test_rewind_does_not_move_checkpoint_backwards(self):
        result = build_history_preview([
            {"date": "2026-09-18", "currentTime": 6000, "timeListening": 600},
            {"date": "2026-09-19", "currentTime": 4500, "timeListening": 900},
        ], duration_minutes=200)

        self.assertEqual(100.0, result["days"][1]["end_position_minutes"])
        self.assertIn("rewind_or_relisten", result["days"][1]["flags"])
        self.assertIn("no_new_progress", result["days"][1]["flags"])
        self.assertEqual("review", result["summary"]["confidence"])

    def test_skips_sessions_without_a_usable_date_or_position(self):
        result = build_history_preview([
            {"currentTime": 100},
            {"date": "2026-09-18", "currentTime": "bad"},
        ], duration_minutes=100)

        self.assertEqual(0, result["summary"]["session_count"])
        self.assertEqual(2, result["summary"]["skipped_session_count"])
        self.assertEqual("insufficient", result["summary"]["confidence"])


if __name__ == "__main__":
    unittest.main()

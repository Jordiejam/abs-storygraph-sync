import unittest

from history import build_history_preview, day_key


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

    def test_flags_a_long_gap_between_listening_days(self):
        result = build_history_preview([
            {"date": "2026-09-01", "currentTime": 600, "timeListening": 600},
            {"date": "2026-09-20", "currentTime": 1200, "timeListening": 600},
        ], duration_minutes=180)

        self.assertNotIn("gap", result["days"][0]["flags"])
        self.assertIn("gap", result["days"][1]["flags"])

    def test_does_not_flag_a_short_gap_between_listening_days(self):
        result = build_history_preview([
            {"date": "2026-09-01", "currentTime": 600, "timeListening": 600},
            {"date": "2026-09-05", "currentTime": 1200, "timeListening": 600},
        ], duration_minutes=180)

        self.assertNotIn("gap", result["days"][1]["flags"])

    def test_flags_a_large_jump_far_exceeding_listening_time(self):
        result = build_history_preview([
            {"date": "2026-09-18", "currentTime": 60, "timeListening": 60},
            {"date": "2026-09-19", "currentTime": 6000, "timeListening": 120},
        ], duration_minutes=180)

        self.assertIn("large_jump", result["days"][1]["flags"])

    def test_does_not_flag_a_jump_proportional_to_listening_time(self):
        result = build_history_preview([
            {"date": "2026-09-18", "currentTime": 1800, "timeListening": 1750},
        ], duration_minutes=180)

        self.assertNotIn("large_jump", result["days"][0]["flags"])


class DayKeyTests(unittest.TestCase):
    def test_is_stable_for_identical_inputs(self):
        self.assertEqual(day_key("2026-09-18", 60.0), day_key("2026-09-18", 60.0))

    def test_distinguishes_date_and_end_position(self):
        base = day_key("2026-09-18", 60.0)
        self.assertNotEqual(base, day_key("2026-09-19", 60.0))
        self.assertNotEqual(base, day_key("2026-09-18", 61.0))

    def test_stays_readable_in_the_stored_state_file(self):
        self.assertEqual("2026-09-18@60.0", day_key("2026-09-18", 60.0))

    def test_tolerates_a_non_numeric_position_from_a_client_payload(self):
        self.assertEqual("2026-09-18@0.0", day_key("2026-09-18", None))
        self.assertEqual("2026-09-18@60.0", day_key("2026-09-18", "60"))


if __name__ == "__main__":
    unittest.main()

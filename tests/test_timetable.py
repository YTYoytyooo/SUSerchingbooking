from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from bookpiano import core
from bookpiano.feishu import FeishuError, publish_snapshot
from bookpiano.timetable import LABEL, occupied_rows, read_grid


def grid():
    # Independent fixture transcribed from the user's actual timetable.
    labels = ["钢琴预约", "8:30-9:00", "9:00-9:30", "9:30-10:00", "10:00-10:30", "10:30-11:00",
              "11:00-11:30", "11:30-12:00", "12:00-12:30", "中午请勿使用！", "14:00-14:30", "14:30-15:00",
              "15:00-15:30", "15:30-16:00", "16:00-16:30", "16:30-17:00", "17:00-17:30", "17:30-18:00",
              "18:00-18:30", "18:30-19:00", "19:00-19:30", "19:30-20:00", "20:00-20:30", "20:30-21:00",
              "21:00-21:30", "21:30-22:00"]
    rows = [[label, "", ""] for label in labels]
    rows[0][1] = "B1保持不变"
    rows[9][1] = "B10保持不变"
    rows[2][2] = "C3保持不变"
    return rows


class GridMemory:
    def __init__(self):
        self.cells = grid()
        self.writes = []
        self.fail_on_write = None

    def read_values(self, token, range_name):
        assert token == "token" and range_name == "piano!A1:C26"
        return deepcopy(self.cells)

    def write_values(self, token, range_name, values):
        assert token == "token"
        self.writes.append(range_name)
        if range_name == "piano!C2:C2":
            self.cells[1][2] = values[0][0]
        else:
            match = re.fullmatch(r"piano!B(\d+):B(\d+)", range_name)
            assert match
            low, high = map(int, match.groups())
            assert 2 <= low <= high <= 9 or 11 <= low <= high <= 26
            for number, value in zip(range(low, high + 1), values, strict=True):
                self.cells[number - 1][1] = value[0]
        if self.fail_on_write == len(self.writes):
            raise FeishuError("synthetic response timeout after commit", retryable=True)


class TimetableTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.client = GridMemory()
        self.now = datetime(2026, 9, 25, 14, tzinfo=core.CST)
        clock = patch("bookpiano.core.now_cst", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.target = {"layout": "timetable", "spreadsheet_token": "token", "sheet_id": "piano",
                       "sheet_title": "Sheet1", "conflict_policy": "meeting_wins", "include_pending": True}
        self.snapshot = {"date": "2026-09-25", "fetched_at": self.now.isoformat(), "bookings": []}

    def meeting(self, start, end, state=9):
        self.snapshot["bookings"].append({"start": self.snapshot["date"] + "T" + start + "+08:00",
                                          "end": self.snapshot["date"] + "T" + end + "+08:00", "state_code": state})

    def publish(self):
        return publish_snapshot(self.client, self.snapshot, self.target, self.root)

    def test_partial_overlap_boundaries_and_pending_are_marked(self):
        self.meeting("09:15:00", "10:10:00", state=0)
        self.meeting("14:00:00", "14:30:00", state=8)
        self.meeting("15:00:00", "16:00:00", state=4)
        self.assertEqual(occupied_rows(self.snapshot, True), {"3", "4", "5", "11"})
        self.assertEqual(occupied_rows(self.snapshot, False), set())
        result = self.publish()
        self.assertEqual(result["occupied_slots"], 4)
        self.assertEqual([i + 1 for i, row in enumerate(self.client.cells) if row[1] == LABEL], [3, 4, 5, 11])

    def test_midday_and_cross_midnight_never_mark_closed_slots(self):
        self.meeting("12:00:00", "14:30:00")
        self.snapshot["bookings"].append({"start": "2026-09-24T23:00:00+08:00", "end": "2026-09-25T09:00:00+08:00", "state_code": 1})
        self.assertEqual(occupied_rows(self.snapshot, True), {"2", "9", "11"})

    def test_next_day_clears_every_old_manual_entry_and_preserves_other_cells(self):
        original = deepcopy(self.client.cells)
        self.client.cells[1][2] = "2026-09-24"
        for number in list(range(2, 10)) + list(range(11, 27)):
            self.client.cells[number - 1][1] = "昨天手填" + str(number)
        self.meeting("09:00:00", "09:30:00")
        self.assertTrue(self.publish()["reset_previous_day"])
        for number in list(range(2, 10)) + list(range(11, 27)):
            self.assertEqual(self.client.cells[number - 1][1], LABEL if number == 3 else "")
        self.assertEqual([row[0] for row in self.client.cells], [row[0] for row in original])
        for number, column in [(1, 1), (10, 1), (3, 2)]:
            self.assertEqual(self.client.cells[number - 1][column], original[number - 1][column])
        self.assertEqual(self.client.cells[1][2], "2026-09-25")
        self.assertFalse((self.root / "feishu-backups").exists())
        self.assertNotIn("昨天手填", (self.root / "timetable-state.json").read_text(encoding="utf-8"))

    def test_same_day_manual_is_retained_except_meeting_conflicts(self):
        self.client.cells[1][2] = "2026-09-25"
        self.client.cells[1][1] = "保留的手填预约"
        self.client.cells[2][1] = "直接覆盖的手填预约"
        self.client.cells[3][1] = LABEL
        self.meeting("09:00:00", "09:30:00")
        result = self.publish()
        self.assertFalse(result["reset_previous_day"])
        self.assertEqual(self.client.cells[1][1], "保留的手填预约")
        self.assertEqual(self.client.cells[2][1], LABEL)
        self.assertEqual(self.client.cells[3][1], "")
        self.assertEqual(result["conflict_cells"], ["B3"])

    def test_retry_after_partial_daily_reset_does_not_clear_new_manual_booking(self):
        self.client.cells[1][2] = "2026-09-24"
        self.client.cells[1][1] = "昨天上午预约"
        self.client.cells[10][1] = "昨天下午预约"
        self.client.fail_on_write = 1
        with self.assertRaises(FeishuError):
            self.publish()
        pending_text = (self.root / "timetable-state.json").read_text(encoding="utf-8")
        self.assertNotIn("昨天上午预约", pending_text)
        self.assertNotIn("昨天下午预约", pending_text)
        self.client.cells[2][1] = "刚填的当天预约"
        self.client.fail_on_write = None
        self.assertTrue(self.publish()["verified"])
        self.assertEqual(self.client.cells[2][1], "刚填的当天预约")
        self.assertEqual(self.client.writes.count("piano!B2:B2"), 1)

    def test_changed_pending_cell_stops_instead_of_deleting_new_content(self):
        self.client.cells[1][2] = "2026-09-24"
        self.client.cells[1][1] = "old manual"
        self.client.fail_on_write = 1
        with self.assertRaises(FeishuError):
            self.publish()
        self.client.cells[1][1] = "new manual"
        self.client.fail_on_write = None
        with self.assertRaises(FeishuError):
            self.publish()
        self.assertEqual(self.client.cells[1][1], "new manual")

    def test_new_day_empty_schedule_clears_manual_added_after_previous_success(self):
        self.publish()
        self.client.cells[25][1] = "稍后手填"
        self.now += timedelta(days=1)
        self.snapshot.update(date="2026-09-26", fetched_at=self.now.isoformat())
        self.assertTrue(self.publish()["reset_previous_day"])
        self.assertEqual(self.client.cells[25][1], "")
        self.assertEqual(self.client.cells[1][2], "2026-09-26")

    def test_layout_change_or_unknown_status_stops_before_any_write(self):
        self.client.cells[2][0] = "9:30-10:00"
        with self.assertRaises(FeishuError):
            self.publish()
        self.client.cells = grid()
        self.meeting("09:00:00", "10:00:00", state=None)
        with self.assertRaises(FeishuError):
            self.publish()
        self.assertEqual(self.client.writes, [])

    def test_identical_retry_does_not_write_again(self):
        self.meeting("09:00:00", "10:00:00")
        self.publish()
        count = len(self.client.writes)
        self.publish()
        self.assertEqual(len(self.client.writes), count)

    def test_existing_typed_date_serial_is_recognized_as_today(self):
        self.client.cells[1][2] = 46290  # Actual Feishu C2 value observed for 2026-09-25.
        self.client.cells[1][1] = "当天手填预约"
        result = self.publish()
        self.assertFalse(result["reset_previous_day"])
        self.assertEqual(self.client.cells[1][1], "当天手填预约")
        self.assertEqual(self.client.writes, [])

    def test_full_pipeline_maps_school_response_and_records_sheet1_success(self):
        from bookpiano.pipeline import run_pipeline, settings_fingerprint
        config = dict(core.default_config(), room_id="room", verified=True)
        config["verified_fingerprint"] = core.fingerprint(config)
        core.write_json(self.root / "config.json", config)
        settings = {"enabled": True, "verified": True, "url": "https://example.feishu.cn/wiki/test", "target": self.target}
        settings["verified_fingerprint"] = settings_fingerprint(settings)
        core.write_json(self.root / "feishu.json", settings)
        calls = []
        def fetch(*_):
            calls.append(1)
            return {"IsSuccess": True, "Data": {"rows": [{"MeetingID": "m1", "MeetingRoomID": "room",
                "StartDateTime": "2026-09-25T09:15:00+08:00", "EndDateTime": "2026-09-25T10:10:00+08:00",
                "Topical": "会议", "State": 8}]}}
        def publish(snapshot, target, root):
            return publish_snapshot(self.client, snapshot, target, root)
        result = run_pipeline(self.root, fetch, publish, now=self.now)
        self.assertEqual(result.status, "success")
        self.assertIn("Sheet1", result.message)
        self.assertEqual([i + 1 for i, row in enumerate(self.client.cells) if row[1] == LABEL], [3, 4, 5])
        sync = core.load_json(self.root / "sync-state.json")
        self.assertTrue(sync["receipt"]["verified"])
        self.assertEqual(sync["receipt"]["occupied_slots"], 3)
        writes = len(self.client.writes)
        self.assertEqual(run_pipeline(self.root, fetch, publish, now=self.now + timedelta(minutes=20)).status, "already_done")
        self.assertEqual((len(calls), len(self.client.writes)), (1, writes))


if __name__ == "__main__":
    unittest.main()

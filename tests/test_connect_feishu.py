import unittest
from unittest.mock import MagicMock

from bookpiano.connect_feishu import bind_target
from bookpiano.feishu import FeishuError
from bookpiano.timetable import SLOTS


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.resolve_wiki.return_value = {"obj_type": "sheet", "obj_token": "token"}
        self.client.list_sheets.return_value = [{"sheet_id": "piano", "title": "Sheet1", "grid_properties": {"row_count": 200, "column_count": 20}}]
        self.values = [["", "", ""] for _ in range(26)]
        for row, (start, end) in SLOTS.items():
            self.values[row - 1][0] = f"{start // 60}:{start % 60:02d}-{end // 60}:{end % 60:02d}"
        self.values[1][2] = "2026-09-25"
        self.client.read_values.return_value = self.values
        self.settings = {"url": "https://x.feishu.cn/wiki/test", "sheet_title": "Sheet1"}

    def test_binds_existing_sheet1_and_does_not_publish_data(self):
        target = bind_target(self.client, self.settings)
        self.assertEqual(target["sheet_id"], "piano")
        self.assertEqual(target["layout"], "timetable")
        self.assertTrue(target["include_pending"])
        self.client.add_sheet.assert_not_called()
        self.client.write_values.assert_not_called()

    def test_existing_manual_bookings_do_not_prevent_binding(self):
        self.values[1][1] = "手填预约"
        self.assertEqual(bind_target(self.client, self.settings)["sheet_id"], "piano")
        self.client.add_sheet.assert_not_called()
        self.client.write_values.assert_not_called()

    def test_missing_sheet_does_not_create_a_replacement(self):
        self.client.list_sheets.return_value = []
        with self.assertRaises(FeishuError):
            bind_target(self.client, self.settings)
        self.client.write_values.assert_not_called()
        self.client.add_sheet.assert_not_called()

    def test_non_spreadsheet_stops_before_modifying_anything(self):
        self.client.resolve_wiki.return_value = {"obj_type": "docx", "obj_token": "token"}
        with self.assertRaises(FeishuError):
            bind_target(self.client, self.settings)
        self.client.list_sheets.assert_not_called()


if __name__ == "__main__":
    unittest.main()

from copy import deepcopy
from datetime import datetime
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from bookpiano import core
from bookpiano.feishu import FeishuClient, FeishuError, MARKER, publish_snapshot


class SheetMemory:
    def __init__(self):
        self.cells = []
        self.writes = 0
        self.fail_after_write = False
        self.mismatch = False
        self.backup_root = None

    def read_values(self, *_):
        rows = deepcopy(self.cells)
        if self.mismatch and self.writes:
            rows[4][4] = "other content"
        # Feishu may omit trailing empty rows and cells.
        for row in rows:
            while row and row[-1] == "":
                row.pop()
        while rows and not rows[-1]:
            rows.pop()
        return rows

    def write_values(self, token, range_name, values):
        assert list((self.backup_root / "feishu-backups").glob("*.json")), "backup must precede write"
        self.writes += 1
        self.cells = deepcopy(values)
        if self.fail_after_write:
            raise FeishuError("synthetic timeout after server commit", retryable=True)
        return {"updatedCells": sum(map(len, values))}


class PublicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.client = SheetMemory()
        self.client.backup_root = self.root
        self.target = {"spreadsheet_token": "token", "sheet_id": "sheet", "managed_rows": 200}
        config = dict(core.default_config(), room_id="room")
        self.snapshot = core.build_snapshot({"IsSuccess": True, "Data": {"rows": []}}, config,
                                             datetime(2026, 9, 25, 5, tzinfo=core.CST))

    def publish(self):
        return publish_snapshot(self.client, self.snapshot, self.target, self.root)

    def test_empty_day_replaces_old_rows_in_one_write_and_verifies(self):
        self.client.cells = [[MARKER], ["old metadata"]] + [["old booking"] for _ in range(20)]
        receipt = self.publish()
        self.assertTrue(receipt["verified"])
        self.assertEqual(self.client.writes, 1)
        self.assertEqual(self.client.cells[4][4], "当天暂无预约")
        self.assertTrue(all(not any(row) for row in self.client.cells[5:]))
        self.assertEqual(len(list((self.root / "feishu-backups").glob("*.json"))), 1)

    def test_unrelated_content_is_not_overwritten(self):
        self.client.cells = [["manual notes"]]
        with self.assertRaises(FeishuError):
            self.publish()
        self.assertEqual(self.client.writes, 0)

    def test_uncertain_write_recovers_by_reading_without_second_write(self):
        self.client.fail_after_write = True
        with self.assertRaises(FeishuError):
            self.publish()
        self.client.fail_after_write = False
        receipt = self.publish()
        self.assertTrue(receipt["verified"])
        self.assertTrue(receipt["unchanged"])
        self.assertEqual(self.client.writes, 1)

    def test_readback_mismatch_is_failure(self):
        self.client.mismatch = True
        with self.assertRaises(FeishuError) as failure:
            self.publish()
        self.assertTrue(failure.exception.retryable)

    def test_formula_like_titles_are_plain_text(self):
        self.snapshot["bookings"] = [{"start": "2026-09-25T09:00:00+08:00", "end": "2026-09-25T10:00:00+08:00",
                                      "title": "=IMPORTXML(\"https://example.com\")", "state": "审批中"}]
        self.publish()
        self.assertEqual(self.client.cells[4][4][0], "\u200b")

    def test_capacity_failure_happens_before_any_read_or_write(self):
        self.target["managed_rows"] = 10
        self.snapshot["bookings"] = [{"start": "2026-09-25T09:00:00+08:00", "end": "2026-09-25T10:00:00+08:00",
                                      "title": "meeting", "state": "approved"} for _ in range(10)]
        with self.assertRaises(FeishuError):
            self.publish()
        self.assertEqual(self.client.writes, 0)


class ClientContractTests(unittest.TestCase):
    def setUp(self):
        self.client = FeishuClient("synthetic-id", "synthetic-secret")

    def test_token_exchange_and_put_use_exact_origin_and_body(self):
        calls = []
        responses = [{"code": 0, "tenant_access_token": "synthetic-token", "expire": 7200},
                     {"code": 0, "data": {"updatedCells": 1}}]
        def send(request, timeout):
            calls.append(request)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(responses.pop(0)).encode()
            return response
        opener = MagicMock()
        opener.open.side_effect = send
        with patch("bookpiano.feishu.urllib.request.build_opener", return_value=opener):
            self.client.write_values("token", "sheet!A1:A1", [["test"]])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].full_url, "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal")
        self.assertEqual(json.loads(calls[0].data), {"app_id": "synthetic-id", "app_secret": "synthetic-secret"})
        self.assertIsNone(calls[0].get_header("Authorization"))
        self.assertEqual(calls[1].method, "PUT")
        self.assertEqual(calls[1].get_header("Authorization"), "Bearer synthetic-token")
        self.assertEqual(json.loads(calls[1].data), {"valueRange": {"range": "sheet!A1:A1", "values": [["test"]]}})

    def test_redirect_or_auth_failure_has_no_retry_and_no_raw_secret(self):
        self.client.token = "synthetic-token"
        for status in [302, 403, 429, 503]:
            opener = MagicMock()
            opener.open.side_effect = urllib.error.HTTPError("https://open.feishu.cn", status, "synthetic-secret", {}, None)
            with patch("bookpiano.feishu.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(FeishuError) as error:
                    self.client.list_sheets("token")
            opener.open.assert_called_once()
            self.assertNotIn("synthetic-secret", str(error.exception))
            self.assertEqual(error.exception.retryable, status in {429, 503})

    def test_public_wiki_url_is_resolved_through_fixed_api_not_user_host(self):
        with patch.object(self.client, "_data", return_value={"node": {"obj_type": "sheet", "obj_token": "sheet-token"}}) as call:
            self.assertEqual(self.client.resolve_wiki("https://example.feishu.cn/wiki/WikiToken?from=copy")["obj_token"], "sheet-token")
        call.assert_called_once_with("GET", "/open-apis/wiki/v2/spaces/get_node?token=WikiToken")

    def test_http_400_retains_code_operation_and_scope_without_secret(self):
        self.client.token = "synthetic-token"
        payload = {"code": 99991672, "msg": "synthetic-secret must never be echoed", "error": {
            "permission_violations": [{"type": "action_scope_required", "subject": "wiki:wiki:readonly"}]}}
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("https://open.feishu.cn", 400, "bad request", {}, io.BytesIO(json.dumps(payload).encode()))
        with patch("bookpiano.feishu.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(FeishuError) as failure:
                self.client.resolve_wiki("https://example.feishu.cn/wiki/test")
        message = str(failure.exception)
        for expected in ["HTTP 400", "99991672", "解析知识库链接", "wiki:wiki:readonly"]:
            self.assertIn(expected, message)
        self.assertNotIn("synthetic-secret", message)
        opener.open.assert_called_once()

    def test_add_sheet_confirms_actual_capacity_from_metadata(self):
        expected = {"sheet_id": "new", "grid_properties": {"row_count": 200, "column_count": 26}}
        with patch.object(self.client, "_data", return_value={"replies": [{"addSheet": {"properties": {"sheetId": "new"}}}]}) as request:
            with patch.object(self.client, "list_sheets", return_value=[expected]):
                self.assertEqual(self.client.add_sheet("token", "自动更新"), expected)
        self.assertEqual(request.call_args.args[2], {"requests": [{"addSheet": {"properties": {"title": "自动更新"}}}]})

    def test_malformed_empty_response_is_not_an_empty_sheet(self):
        with patch.object(self.client, "_data", return_value={"valueRange": {}}):
            with self.assertRaises(FeishuError):
                self.client.read_values("token", "sheet!A1:G200")


if __name__ == "__main__":
    unittest.main()

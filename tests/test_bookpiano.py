import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from bookpiano import core


def moment(hour=9, day=24):
    return datetime(2026, 9, day, hour, 0, tzinfo=core.CST)


def response(rows=None):
    return {"IsSuccess": True, "Data": {"rows": rows if rows is not None else [
        {"MeetingID": "m1", "MeetingRoomID": "room1", "Topical": "读书会",
         "StartDateTime": "2026-09-24T09:00:00", "EndDateTime": "2026-09-24T10:00:00", "State": 9}
    ]}}


class LocalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = core.default_config()
        self.config.update(room_id="room1", verified=True)
        self.config["verified_fingerprint"] = core.fingerprint(self.config)
        core.write_json(self.root / "config.json", self.config)
        self.calls = 0

    def fetch(self, config, day):
        self.calls += 1
        return response()

    def run_update(self, now=None, fetch=None, force=False):
        return core.run_update(self.root, fetch or self.fetch, now=now or moment(), force=force)

    def test_missed_run_then_reboot_does_not_query_again(self):
        self.assertEqual(self.run_update().status, "success")
        self.assertEqual(self.run_update(now=moment(12)).status, "already_done")
        self.assertEqual(self.calls, 1)
        self.assertEqual(core.load_json(self.root / "state.json")["last_success_date"], "2026-09-24")

    def test_before_five_makes_no_request(self):
        self.assertEqual(self.run_update(now=moment(4)).status, "not_due")
        self.assertEqual(self.calls, 0)

    def test_failure_preserves_previous_and_retry_budget_survives_restart(self):
        self.run_update()
        original = (self.root / "latest.json").read_bytes()
        def failing(config, day):
            self.calls += 1
            raise core.FetchError("网络连接失败", retryable=True)
        self.assertEqual(self.run_update(now=moment(9, 25), fetch=failing).status, "retry_pending")
        self.assertEqual(self.run_update(now=moment(9, 25), fetch=failing).status, "cooldown")
        self.assertEqual(self.run_update(now=moment(10, 25), fetch=failing).status, "failed")
        self.assertEqual(self.run_update(now=moment(11, 25), fetch=failing).status, "daily_limit")
        self.assertEqual(self.calls, 3)  # Yesterday: 1. Today: exactly 2.
        self.assertEqual((self.root / "latest.json").read_bytes(), original)
        self.assertIn("网络连接失败", (self.root / "查看预约.html").read_text(encoding="utf-8"))

    def test_empty_success_replaces_old_rows(self):
        self.run_update()
        result = self.run_update(now=moment(10), fetch=lambda *_: response([]), force=True)
        self.assertEqual(result.status, "success")
        latest = core.load_json(self.root / "latest.json")
        self.assertEqual(latest["bookings"], [])
        self.assertIn("当天暂无预约", (self.root / "查看预约.html").read_text(encoding="utf-8"))

    def test_auth_failure_does_not_auto_retry_even_after_restart(self):
        def failing(*_):
            self.calls += 1
            raise core.FetchError("请重新登录", retryable=False, kind="auth")
        self.assertEqual(self.run_update(fetch=failing).status, "failed")
        self.assertEqual(self.run_update(now=moment(12), fetch=failing).status, "blocked")
        self.assertEqual(self.calls, 1)

    def test_pending_export_recovers_without_fetch(self):
        sample = core.build_snapshot(response(), self.config, moment())
        core.write_json(self.root / "pending.json", sample)
        result = self.run_update()
        self.assertEqual(result.status, "success")
        self.assertEqual(self.calls, 0)

    def test_state_corruption_fails_closed(self):
        (self.root / "state.json").write_text("{bad", encoding="utf-8")
        with self.assertRaises(core.LocalError):
            self.run_update()
        self.assertEqual(self.calls, 0)

    def test_manual_refresh_obeys_cooldown(self):
        self.run_update()
        self.assertEqual(self.run_update(force=True).status, "cooldown")
        self.assertEqual(self.calls, 1)

    def test_no_requests_until_configuration_is_verified(self):
        self.config["verified"] = False
        core.write_json(self.root / "config.json", self.config)
        self.assertEqual(self.run_update().status, "setup_required")
        self.assertEqual(self.calls, 0)

    def test_lock_prevents_duplicate_execution(self):
        with core.RunLock(self.root):
            self.assertEqual(self.run_update().status, "busy")
        self.assertEqual(self.calls, 0)

    def test_pre_five_manual_update_does_not_replace_five_am_update(self):
        self.assertEqual(self.run_update(now=moment(4), force=True).status, "success")
        self.assertEqual(self.run_update(now=moment(5)).status, "success")
        self.assertEqual(self.calls, 2)

    def test_changed_room_requires_new_verification(self):
        self.config["room_id"] = "unverified-room"
        core.write_json(self.root / "config.json", self.config)
        self.assertEqual(self.run_update().status, "setup_required")
        self.assertEqual(self.calls, 0)

    def test_auth_block_persists_across_days(self):
        def failing(*_):
            self.calls += 1
            raise core.FetchError("请重新登录", kind="auth")
        self.run_update(fetch=failing)
        self.assertEqual(self.run_update(now=moment(9, 25), fetch=failing).status, "blocked")
        self.assertEqual(self.calls, 1)

    def test_rate_limit_stops_today_but_allows_next_day(self):
        def limited(*_):
            self.calls += 1
            raise core.FetchError("今天停止请求", kind="rate_limit")
        self.assertEqual(self.run_update(fetch=limited).status, "failed")
        self.assertEqual(self.run_update(now=moment(12), fetch=limited).status, "blocked")
        def empty(*_):
            self.calls += 1
            return response([])
        self.assertEqual(self.run_update(now=moment(9, 25), fetch=empty).status, "success")
        self.assertEqual(self.calls, 2)

    def test_pre_five_cannot_spend_both_daily_requests(self):
        self.run_update(now=moment(3), force=True)
        self.assertEqual(self.run_update(now=moment(4), force=True).status, "reserved")
        self.assertEqual(self.run_update(now=moment(5)).status, "success")
        self.assertEqual(self.calls, 2)

    def test_request_crossing_midnight_is_not_published(self):
        moments = iter([moment(23), moment(0, 25)])
        result = core.run_update(self.root, self.fetch, clock=lambda: next(moments))
        self.assertEqual(result.status, "retry_pending")
        self.assertFalse((self.root / "latest.json").exists())

    def test_crash_after_request_respects_budget_and_cooldown(self):
        def interrupted(*_):
            self.calls += 1
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_update(fetch=interrupted)
        self.assertEqual(self.run_update().status, "cooldown")
        self.assertEqual(core.load_json(self.root / "state.json")["attempts"], 1)
        self.assertEqual(self.calls, 1)

    def test_published_snapshot_recovers_lost_state_without_fetch(self):
        sample = core.build_snapshot(response(), self.config, moment())
        core.write_json(self.root / "latest.json", sample)
        self.assertEqual(self.run_update().status, "success")
        self.assertEqual(self.calls, 0)

    def test_publication_failure_can_resume_without_another_request(self):
        from unittest.mock import patch
        with patch.object(core, "publish", side_effect=PermissionError("locked file")):
            with self.assertRaises(core.LocalError):
                self.run_update()
        self.assertTrue((self.root / "pending.json").is_file())
        self.assertNotIn("last_success_date", core.load_json(self.root / "state.json"))
        self.assertEqual(self.run_update().status, "success")
        self.assertEqual(self.calls, 1)


class DataValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = core.default_config()
        self.config.update(room_id="room1", verified=True)

    def test_rejects_failed_malformed_and_partial_responses(self):
        samples = [{"IsSuccess": False, "Data": {"rows": []}},
                   {"IsSuccess": True, "Data": {}},
                   {"IsSuccess": True, "Data": {"rows": [], "total": 4}},
                   response([{"StartDateTime": "bad"}])]
        for sample in samples:
            with self.subTest(sample=sample), self.assertRaises(core.FetchError):
                core.build_snapshot(sample, self.config, moment())

    def test_date_room_and_duration_must_match(self):
        for changes in [{"MeetingRoomID": "other"},
                        {"StartDateTime": "2026-09-25T09:00:00", "EndDateTime": "2026-09-25T10:00:00"},
                        {"EndDateTime": "2026-09-24T08:00:00"}]:
            sample = response()
            sample["Data"]["rows"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(core.FetchError):
                core.build_snapshot(sample, self.config, moment())

    def test_local_z_convention_and_cross_midnight(self):
        sample = response()
        sample["Data"]["rows"][0].update(StartDateTime="2026-09-23T23:30:00.000Z", EndDateTime="2026-09-24T00:30:00.000Z")
        booking = core.build_snapshot(sample, self.config, moment())["bookings"][0]
        self.assertEqual(booking["start"], "2026-09-23T23:30:00+08:00")
        self.assertEqual(booking["end"], "2026-09-24T00:30:00+08:00")

    def test_csv_formula_and_html_injection_are_escaped(self):
        sample = response()
        sample["Data"]["rows"][0]["Topical"] = '=HYPERLINK("evil")<script>alert(1)</script>'
        snapshot = core.build_snapshot(sample, self.config, moment())
        csv_text = core.csv_text(snapshot)
        self.assertIn("'=HYPERLINK", csv_text)
        page = core.html_text(snapshot)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


class HttpContractTests(unittest.TestCase):
    def test_endpoint_payload_and_token_are_exact_and_only_one_request(self):
        from unittest.mock import MagicMock, patch
        config = core.default_config()
        config["room_id"] = "room1"
        from bookpiano.source import learn_profile
        config["source_profile"] = learn_profile(core.QUERY_PATH,
            {"MessageType": 5001, "Data": {"meetingroomid": "room1", "currentdate": "2026-09-24T00:00:00.000Z"}},
            response(), "room1", config["room_name"], moment().date())
        manager = MagicMock()
        manager.__enter__.return_value.read.return_value = json.dumps(response()).encode()
        opener = MagicMock()
        opener.open.return_value = manager
        with patch("bookpiano.core.urllib.request.build_opener", return_value=opener):
            core.query_bookings(config, moment().date(), "not-a-real-session-token")
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, core.ORIGIN + core.QUERY_PATH)
        self.assertEqual(request.get_header("Authorization"), "not-a-real-session-token")
        body = json.loads(request.data)
        self.assertEqual(body["MessageType"], 5001)
        self.assertEqual(body["Data"], {"meetingroomid": "room1", "currentdate": "2026-09-24T00:00:00.000Z"})

    def test_redirect_never_forwards_token(self):
        import urllib.request
        request = urllib.request.Request(core.ORIGIN, headers={"Authorization": "fake"})
        self.assertIsNone(core.NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://other.invalid/"))

    def test_mutation_endpoints_are_rejected_before_network(self):
        with self.assertRaises(core.LocalError):
            core.api_post("/api/SystemApi/AddMeeting", 5002, {}, "fake")


if __name__ == "__main__":
    unittest.main()

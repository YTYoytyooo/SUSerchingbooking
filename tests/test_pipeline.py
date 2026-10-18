import tempfile
from pathlib import Path
from datetime import datetime
import unittest

from bookpiano import core
from bookpiano.pipeline import run_pipeline, settings_fingerprint
from bookpiano.feishu import FeishuError


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        config = core.default_config()
        config.update(room_id="room1", verified=True)
        config["verified_fingerprint"] = core.fingerprint(config)
        core.write_json(self.root / "config.json", config)
        self.settings = {"enabled": True, "verified": True, "url": "https://x.feishu.cn/wiki/test", "target": {"spreadsheet_token": "test", "sheet_id": "test", "managed_rows": 1000}}
        self.settings["verified_fingerprint"] = settings_fingerprint(self.settings)
        core.write_json(self.root / "feishu.json", self.settings)
        self.fetches = 0
        self.writes = 0

    def fetch(self, *_):
        self.fetches += 1
        return {"IsSuccess": True, "Data": {"rows": []}}

    def publish(self, *_):
        self.writes += 1
        return {"verified": True}

    def update(self, minute=0, publisher=None, force=False, ignore_cooldown=False):
        return run_pipeline(self.root, self.fetch, publisher or self.publish,
                            now=datetime(2026, 9, 25, 9, minute, tzinfo=core.CST), force=force,
                            ignore_cooldown=ignore_cooldown)

    def test_manual_override_skips_both_intervals_but_not_daily_cap(self):
        self.assertEqual(self.update().status, "success")
        self.assertEqual(self.update(1, force=True).status, "cooldown")
        self.assertEqual((self.fetches, self.writes), (1, 1))
        self.assertEqual(self.update(1, force=True, ignore_cooldown=True).status, "success")
        self.assertEqual((self.fetches, self.writes), (2, 2))
        self.assertEqual(self.update(2, force=True, ignore_cooldown=True).status, "sync_daily_limit")
        self.assertEqual((self.fetches, self.writes), (2, 2))

    def test_manual_cloud_retry_uses_saved_snapshot_without_school_query(self):
        def fail(*_):
            self.writes += 1
            raise FeishuError("timeout", retryable=True)
        self.assertEqual(self.update(publisher=fail).status, "sync_retry_pending")
        self.assertEqual(self.update(1, force=True, ignore_cooldown=True).status, "success")
        self.assertEqual((self.fetches, self.writes), (1, 2))

    def test_override_never_unblocks_auth_failure(self):
        core.write_json(self.root / "state.json", {"blocked": True, "blocked_kind": "auth"})
        self.assertEqual(self.update(1, force=True, ignore_cooldown=True).status, "blocked")
        self.assertEqual((self.fetches, self.writes), (0, 0))

    def test_override_never_resets_school_daily_budget(self):
        core.write_json(self.root / "state.json", {"attempt_date": "2026-09-25", "attempts": 2})
        self.assertEqual(self.update(1, force=True, ignore_cooldown=True).status, "daily_limit")
        self.assertEqual((self.fetches, self.writes), (0, 0))

    def test_override_requires_explicit_manual_refresh(self):
        with self.assertRaises(core.LocalError):
            self.update(ignore_cooldown=True)
        self.assertEqual((self.fetches, self.writes), (0, 0))

    def test_school_retry_override_is_not_persisted_for_later_runs(self):
        def fail(*_):
            self.fetches += 1
            raise core.FetchError("timeout", retryable=True, kind="network")
        now = datetime(2026, 9, 25, 9, 0, tzinfo=core.CST)
        self.assertEqual(core.run_update(self.root, fail, now=now).status, "retry_pending")
        later = now.replace(minute=1)
        self.assertEqual(core.run_update(self.root, self.fetch, now=later, force=True).status, "cooldown")
        self.assertEqual(core.run_update(self.root, self.fetch, now=later, force=True,
                                         ignore_cooldown=True).status, "success")
        self.assertEqual(self.fetches, 2)
        self.assertNotIn("ignore_cooldown", core.load_json(self.root / "state.json"))

    def test_success_then_reboot_calls_neither_service(self):
        self.assertEqual(self.update().status, "success")
        self.assertEqual(self.update(20).status, "already_done")
        self.assertEqual((self.fetches, self.writes), (1, 1))

    def test_cloud_failure_retains_local_and_only_retries_cloud(self):
        def failed(*_):
            self.writes += 1
            raise FeishuError("network timeout", retryable=True)
        self.assertEqual(self.update(publisher=failed).status, "sync_retry_pending")
        self.assertTrue((self.root / "latest.json").exists())
        self.assertNotIn("success_date", core.load_json(self.root / "sync-state.json"))
        self.assertEqual(self.update(5).status, "cooldown")
        self.assertEqual(self.update(20).status, "success")
        self.assertEqual((self.fetches, self.writes), (1, 2))

    def test_cloud_auth_failure_stops_requests(self):
        def denied(*_):
            self.writes += 1
            raise FeishuError("permission denied", retryable=False)
        self.assertEqual(self.update(publisher=denied).status, "sync_failed")
        self.assertEqual(self.update(20).status, "sync_blocked")
        self.assertEqual((self.fetches, self.writes), (1, 1))

    def test_failed_manual_refresh_does_not_reuse_earlier_cloud_success(self):
        self.assertEqual(self.update().status, "success")
        def fail(*_):
            raise FeishuError("network timeout", retryable=True)
        self.assertEqual(self.update(20, publisher=fail, force=True).status, "sync_failed")
        self.assertEqual(self.update(40).status, "sync_daily_limit")

    def test_pre_five_cloud_retry_preserves_scheduled_update_budget(self):
        def fail(*_):
            raise FeishuError("network timeout", retryable=True)
        result = run_pipeline(self.root, self.fetch, fail,
                              now=datetime(2026, 9, 25, 4, 20, tzinfo=core.CST), force=True)
        self.assertEqual(result.status, "sync_retry_pending")
        result = run_pipeline(self.root, self.fetch, self.publish,
                              now=datetime(2026, 9, 25, 4, 40, tzinfo=core.CST), force=True)
        self.assertEqual(result.status, "sync_reserved")
        self.assertEqual(self.update().status, "success")
        self.assertEqual(self.fetches, 2)

    def test_missing_destination_does_not_query_school(self):
        self.settings["verified"] = False
        core.write_json(self.root / "feishu.json", self.settings)
        self.assertEqual(self.update().status, "destination_required")
        self.assertEqual(self.fetches, 0)

    def test_changed_destination_requires_reconnection_without_school_query(self):
        self.settings["target"]["sheet_id"] = "another"
        core.write_json(self.root / "feishu.json", self.settings)
        self.assertEqual(self.update().status, "destination_required")
        self.assertEqual(self.fetches, 0)


if __name__ == "__main__":
    unittest.main()

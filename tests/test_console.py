from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bookpiano import console, core


class ConsoleWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_cancelled_credentials_stops_before_school_or_cloud(self):
        commands = []
        def run(command):
            commands.append(command)
            return 2 if command == "configure" else 0
        with patch.object(console, "credentials_ready", return_value=False):
            self.assertEqual(console.wizard(self.root, run), 2)
        self.assertEqual(commands, ["prepare", "configure"])

    def test_zero_exit_without_completed_configuration_stops(self):
        commands = []
        with patch.object(console, "credentials_ready", return_value=False):
            result = console.wizard(self.root, lambda command: commands.append(command) or 0)
        self.assertEqual(result, 2)
        self.assertEqual(commands, ["prepare", "configure"])

    def test_cooldown_zero_exit_never_installs_or_claims_success(self):
        commands = []
        with ExitStack() as stack:
            for check in ["credentials_ready", "source_ready", "destination_ready"]:
                stack.enter_context(patch.object(console, check, return_value=True))
            stack.enter_context(patch.object(console, "today_complete", return_value=False))
            result = console.wizard(self.root, lambda command: commands.append(command) or 0)
        self.assertEqual(result, 2)
        self.assertEqual(commands, ["prepare", "update"])

    def test_completed_setup_skips_network_steps_and_preserves_existing_task(self):
        commands = []
        with ExitStack() as stack:
            for check in ["credentials_ready", "source_ready", "destination_ready", "today_complete"]:
                stack.enter_context(patch.object(console, check, return_value=True))
            result = console.wizard(self.root, lambda command: commands.append(command) or 0)
        self.assertEqual(result, 0)
        self.assertEqual(commands, ["prepare", "check-ready", "install-if-missing"])

    def test_resume_from_cloud_connection_does_not_repeat_school_setup(self):
        commands = []
        with ExitStack() as stack:
            for check in ["credentials_ready", "source_ready"]:
                stack.enter_context(patch.object(console, check, return_value=True))
            stack.enter_context(patch.object(console, "destination_ready", side_effect=[False, True]))
            stack.enter_context(patch.object(console, "today_complete", side_effect=[False, True]))
            result = console.wizard(self.root, lambda command: commands.append(command) or 0)
        self.assertEqual(result, 0)
        self.assertEqual(commands, ["prepare", "connect-feishu", "update", "check-ready", "install-if-missing"])

    def test_today_complete_requires_current_after_five_verified_receipt(self):
        now = datetime(2026, 10, 18, 12, tzinfo=core.CST)
        state = {"status": "success", "success_date": "2026-10-18", "receipt": {"verified": True},
                 "source_fingerprint": "source", "target_fingerprint": "target"}
        with patch.object(core, "now_cst", return_value=now), patch.object(core, "fingerprint", return_value="source"), \
                patch.object(console, "settings_fingerprint", return_value="target"):
            for stamp, expected in [("2026-10-17T05:00:00+08:00", False),
                                    ("2026-10-18T04:59:00+08:00", False),
                                    ("2026-10-18T05:00:00+08:00", True)]:
                state["snapshot_fetched_at"] = stamp
                core.write_json(self.root / "sync-state.json", state)
                self.assertEqual(console.today_complete(self.root), expected)
            state["receipt"]["verified"] = False
            core.write_json(self.root / "sync-state.json", state)
            self.assertFalse(console.today_complete(self.root))

    def test_only_explicit_manual_update_forces_school_refresh(self):
        with patch.object(console, "run_process", return_value=0) as run:
            console.action(self.root, "manual-update")
            argv = run.call_args.args[1]
            self.assertEqual(argv[3], "update")
            self.assertIn("--force", argv)
            console.action(self.root, "update")
            self.assertNotIn("--force", run.call_args.args[1])

    def test_menu_exit_never_starts_network_work(self):
        with patch("builtins.input", return_value="0"), patch.object(console, "action") as action:
            self.assertEqual(console.menu(self.root), 0)
            action.assert_not_called()

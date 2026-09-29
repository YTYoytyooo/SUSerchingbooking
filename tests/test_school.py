from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bookpiano import core, school


class SchoolLoginTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.now = datetime(2026, 9, 25, 5, tzinfo=core.CST)
        for name, value in [("bookpiano.core.now_cst", self.now),
                            ("bookpiano.school.load_account", {"username": "test-user", "password": "test-password"})]:
            mock = patch(name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_login_budget_persisted_before_submission_and_survives_failure(self):
        page = MagicMock(url=school.CAS_LOGIN)
        def interrupted():
            self.assertFalse(school.login_available(self.root))
            raise TimeoutError("test timeout")
        page.locator.return_value.click.side_effect = interrupted
        with self.assertRaises(TimeoutError):
            school.submit_account_once(self.root, page)
        with self.assertRaises(core.FetchError):
            school.submit_account_once(self.root, page)
        page.locator.return_value.click.assert_called_once()
        content = (self.root / "auth-state.json").read_text()
        self.assertNotIn("test-password", content)
        self.assertNotIn("test-user", content)
        with patch("bookpiano.core.now_cst", return_value=self.now + timedelta(days=1)):
            self.assertTrue(school.login_available(self.root))

    def test_no_password_fill_outside_exact_login_origin_and_path(self):
        for url in ["http://cas.sustech.edu.cn/cas/login", "https://cas.sustech.edu.cn.example/cas/login",
                    "https://cas.sustech.edu.cn:8443/cas/login", "https://cas.sustech.edu.cn/cas/login-other"]:
            page = MagicMock(url=url)
            with self.subTest(url=url), self.assertRaises(core.FetchError):
                school.submit_account_once(self.root, page)
            page.locator.assert_not_called()

    def test_expired_and_invalid_sessions_are_not_reused(self):
        for hours, invalid, expected in [(1, False, "fake-token"), (7, False, None), (1, True, None)]:
            core.write_json(self.root / "auth-state.json", {"obtained_at": (self.now - timedelta(hours=hours)).isoformat(), "invalid": invalid})
            with patch("bookpiano.school.load_token", return_value="fake-token"):
                self.assertEqual(school.cached_token(self.root), expected)

    def test_missing_profile_never_opens_login(self):
        with patch("bookpiano.school.get_token") as token:
            with self.assertRaises(core.LocalError):
                school.fetch_bookings(self.root, core.default_config(), self.now.date())
            token.assert_not_called()


if __name__ == "__main__":
    unittest.main()

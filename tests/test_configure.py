import unittest

from bookpiano.configure import normalize_target_url


class TargetUrlTests(unittest.TestCase):
    def test_accepts_user_link_and_discards_tracking(self):
        self.assertEqual(
            normalize_target_url(" https://example.feishu.cn/wiki/TestToken/?from=copy#view "),
            "https://example.feishu.cn/wiki/TestToken",
        )

    def test_rejects_wrong_origin_credentials_and_non_wiki_links(self):
        for value in ["", "http://example.feishu.cn/wiki/test",
                      "https://example.feishu.cn.evil.test/wiki/test",
                      "https://user:password@example.feishu.cn/wiki/test",
                      "https://example.feishu.cn:8443/wiki/test",
                      "https://example.feishu.cn:bad/wiki/test",
                      "https://example.feishu.cn/sheets/test"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_target_url(value)

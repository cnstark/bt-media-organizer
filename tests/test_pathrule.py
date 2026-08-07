"""路径规则单测(eq/sub/add/replace + filter/selector + 解析)。"""
import unittest

from src.transfer.pathrule import convert_path, match_path, parse_rule_text


class TestParseRule(unittest.TestCase):
    def test_parse_basic(self):
        text = "# 注释\n/downloads{#**#}/volume1\n\n/other\n  /x {#**#} /y  \n"
        self.assertEqual(parse_rule_text(text),
                         [("/downloads", "/volume1"), ("/other", ""), ("/x", "/y")])

    def test_parse_empty(self):
        self.assertEqual(parse_rule_text(""), [])
        self.assertEqual(parse_rule_text("  \n# only comment\n"), [])


class TestConvertPath(unittest.TestCase):
    def test_eq(self):
        self.assertEqual(convert_path("/data/x", "eq", []), "/data/x")
        self.assertIsNone(convert_path("", "eq", []))

    def test_sub(self):
        rules = [("/downloads", "")]
        self.assertEqual(convert_path("/downloads/movie/a.mkv", "sub", rules), "/movie/a.mkv")
        # 未命中 → None
        self.assertIsNone(convert_path("/other/movie/a.mkv", "sub", rules))
        # 结果为空 → None
        self.assertIsNone(convert_path("/downloads", "sub", rules))

    def test_add(self):
        rules = [("/downloads", "/volume1")]
        self.assertEqual(convert_path("/downloads/movie/a.mkv", "add", rules),
                         "/volume1/downloads/movie/a.mkv")

    def test_replace(self):
        rules = [("/downloads", "/volume1/downloads")]
        self.assertEqual(convert_path("/downloads/movie/a.mkv", "replace", rules),
                         "/volume1/downloads/movie/a.mkv")

    def test_rule_order_and_trailing_slash(self):
        rules = [("/downloads", "/v1"), ("/downloads/sub", "/v2")]
        # 按序匹配,先命中 /downloads
        self.assertEqual(convert_path("/downloads/sub/x", "replace", rules), "/v1/sub/x")
        # 尾部斜杠归一
        self.assertEqual(convert_path("/downloads/", "replace", [("/downloads/", "/v1/")]),
                         "/v1/")


class TestMatchPath(unittest.TestCase):
    def test_filter(self):
        self.assertFalse(match_path("/downloads/tmp/x", ["/downloads/tmp"], []))
        self.assertTrue(match_path("/downloads/movie/x", ["/downloads/tmp"], []))

    def test_selector(self):
        self.assertTrue(match_path("/downloads/movie/x", [], ["/downloads/movie"]))
        self.assertFalse(match_path("/downloads/tv/x", [], ["/downloads/movie"]))
        # 选择器非空时无命中 → False
        self.assertFalse(match_path("/other/x", [], ["/downloads/movie"]))

    def test_selector_with_filter(self):
        self.assertTrue(match_path("/downloads/movie/x", ["/downloads/tmp"], ["/downloads"]))
        self.assertFalse(match_path("/downloads/tmp/x", ["/downloads/tmp"], ["/downloads"]))


if __name__ == "__main__":
    unittest.main()

"""Jackett 匹配器单测(mock httpx + Torznab XML)。"""
import unittest
import xml.etree.ElementTree as ET

import httpx

from src.config import JackettConf
from src.downloaders.base import TorrentInfo
from src.downloaders.bencode import encode
from src.reseed.matcher import JackettMatcher


def make_conf(**kw) -> JackettConf:
    base = dict(url="http://127.0.0.1:9117", api_key="key",
                indexers=["siteA", "siteB"], size_tolerance=0.05,
                per_indexer_delay=0)
    base.update(kw)
    return JackettConf(**base)


def torznab_xml(items: list) -> bytes:
    ns = 'xmlns:torznab="http://torznab.com/schemas/2015/feed"'
    body = [f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" {ns}><channel>']
    for it in items:
        body.append("<item>")
        body.append(f"<title>{it['title']}</title>")
        body.append(f"<size>{it['size']}</size>")
        body.append(f"<link>{it['link']}</link>")
        for name, value in it.get("attrs", []):
            body.append(f'<torznab:attr name="{name}" value="{value}"/>')
        body.append("</item>")
    body.append("</channel></rss>")
    return "".join(body).encode()


def make_matcher(handler, **kw) -> JackettMatcher:
    conf = make_conf(**kw)
    m = JackettMatcher(conf)
    m._client = httpx.Client(base_url=conf.url.rstrip("/"),
                             transport=httpx.MockTransport(handler))
    return m


class TestJackettMatcher(unittest.TestCase):
    def test_match_with_infohash_attr_no_download(self):
        """Torznab 直接返回 infohash → 不下载候选。"""
        requested = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            if "siteA" in request.url.path:
                content = torznab_xml([{
                    "title": "Movie.2024.1080p", "size": 1000,
                    "link": "http://siteA/dl/1",
                    "attrs": [("infohash", "H1"), ("seeders", "10")],
                }])
            else:
                content = torznab_xml([])
            return httpx.Response(200, content=content)

        m = make_matcher(handler)
        t = TorrentInfo(hash="h1", name="Movie.2024.1080p", save_path="/d", content_path="/d", size=1000)
        cands = m.match(t, 10)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].indexer, "siteA")
        self.assertEqual(cands[0].info_hash, "H1")
        # 只请求了两个白名单索引器,没有下载请求
        self.assertEqual(len(requested), 2)

    def test_match_downloads_candidate_without_attr(self):
        """无 infohash 属性 → 下载候选种子本地比对。"""
        torrent_bytes = encode({b"info": {b"name": b"x", b"length": 100}})
        from src.downloaders.bencode import info_hash
        target_hash = info_hash(torrent_bytes)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/results/torznab"):
                return httpx.Response(200, content=torznab_xml([{
                    "title": "Movie.2024.1080p", "size": 1000,
                    "link": "http://siteA/dl/1", "attrs": [],
                }]))
            # 下载请求
            self.assertIn("/dl/1", str(request.url))
            return httpx.Response(200, content=torrent_bytes)

        m = make_matcher(handler, indexers=["siteA"])
        t = TorrentInfo(hash=target_hash, name="Movie.2024.1080p",
                        save_path="/d", content_path="/d", size=1000)
        cands = m.match(t, 10)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].info_hash, "")

    def test_size_tolerance(self):
        def handler(request):
            return httpx.Response(200, content=torznab_xml([
                {"title": "A", "size": 1200, "link": "http://s/1",
                 "attrs": [("infohash", "h1")]},     # 超容差(20%)
                {"title": "B", "size": 1030, "link": "http://s/2",
                 "attrs": [("infohash", "h2")]},     # 3% 容差内
            ]))

        m = make_matcher(handler, indexers=["siteA"])
        t = TorrentInfo(hash="h2", name="A", save_path="/d", content_path="/d", size=1000)
        cands = m.match(t, 10)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].title, "B")

    def test_infohash_mismatch_dropped(self):
        def handler(request):
            return httpx.Response(200, content=torznab_xml([
                {"title": "A", "size": 1000, "link": "http://s/1",
                 "attrs": [("infohash", "other")]},
            ]))

        m = make_matcher(handler, indexers=["siteA"])
        t = TorrentInfo(hash="h1", name="A", save_path="/d", content_path="/d", size=1000)
        self.assertEqual(m.match(t, 10), [])

    def test_whitelist_only(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, content=torznab_xml([]))

        m = make_matcher(handler, indexers=["siteB"])
        t = TorrentInfo(hash="h1", name="A", save_path="/d", content_path="/d", size=1000)
        m.match(t, 10)
        self.assertEqual(paths, ["/api/v2.0/indexers/siteB/results/torznab"])

    def test_download_appends_apikey_for_jackett_host(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            return httpx.Response(200, content=b"TORRENT")

        m = make_matcher(handler)
        data = m.download("http://127.0.0.1:9117/dl/1")
        self.assertEqual(data, b"TORRENT")
        self.assertIn("apikey=key", captured["url"])
        # 外部链接不追加
        captured.clear()
        data = m.download("http://siteA/dl/1")
        self.assertEqual(data, b"TORRENT")
        self.assertNotIn("apikey", captured["url"])


if __name__ == "__main__":
    unittest.main()

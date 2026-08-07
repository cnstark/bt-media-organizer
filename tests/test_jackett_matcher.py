"""Jackett 匹配器单测(mock httpx + Torznab XML + 文件级匹配)。"""
import unittest

import httpx

from src.config import JackettConf
from src.downloaders.base import TorrentInfo
from src.downloaders.bencode import encode
from src.reseed.matcher import JackettMatcher, match_ratio


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


def make_torrent_with_files(files, hash_="h1", size=1000, name="Movie.2024.1080p") -> TorrentInfo:
    return TorrentInfo(hash=hash_, name=name, save_path="/d", content_path="/d", size=size)


def make_matcher(handler, **kw) -> JackettMatcher:
    conf = make_conf(**kw)
    m = JackettMatcher(conf)
    m._client = httpx.Client(base_url=conf.url.rstrip("/"),
                             transport=httpx.MockTransport(handler))
    return m


LOCAL_FILES = [("Movie.2024.1080p.mkv", 1000), ("Movie.2024.1080p.zh.srt", 10)]


class TestMatchRatio(unittest.TestCase):
    def test_full_match(self):
        self.assertEqual(match_ratio(LOCAL_FILES, LOCAL_FILES), 1.0)

    def test_dir_diff_ignored(self):
        """跨站重新打包:目录结构不同但 basename+size 一致 → 匹配。"""
        local = [("Movie/Movie.2024.1080p.mkv", 1000), ("Movie/sub/zh.srt", 10)]
        cand = [("Movie.2024.1080p.mkv", 1000), ("sub/zh.srt", 10)]
        self.assertEqual(match_ratio(local, cand), 1.0)

    def test_partial_extra_candidate_files(self):
        """候选多出 sample/nfo 不影响(分母是本地)。"""
        cand = LOCAL_FILES + [("sample.mkv", 50), ("movie.nfo", 1)]
        self.assertEqual(match_ratio(LOCAL_FILES, cand), 1.0)

    def test_partial_missing(self):
        cand = LOCAL_FILES[:1]  # 少一个文件
        self.assertEqual(match_ratio(LOCAL_FILES, cand), 0.5)

    def test_size_mismatch(self):
        cand = [("Movie.2024.1080p.mkv", 999), ("Movie.2024.1080p.zh.srt", 10)]
        self.assertEqual(match_ratio(LOCAL_FILES, cand), 0.5)

    def test_empty(self):
        self.assertEqual(match_ratio([], LOCAL_FILES), 0.0)


class TestJackettMatcher(unittest.TestCase):
    def test_same_infohash_fast_hit_no_download(self):
        """Torznab 返回同 infohash → 零下载直接命中。"""
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
        t = make_torrent_with_files(LOCAL_FILES, hash_="h1")
        with unittest.mock.patch("src.reseed.matcher.time.sleep"):
            cands = m.match(t, LOCAL_FILES, 10)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].info_hash, "H1")
        # siteA 命中 1 次搜索;siteB 空结果 → 重试 1 次,共 2 次;无下载请求
        self.assertEqual(len(requested), 3)
        self.assertNotIn("/dl/1", requested[0])

    def test_diff_infohash_matches_by_files(self):
        """不同站重新打包:infohash 不同但文件一致 → 下载比对命中。"""
        torrent_bytes = encode({b"info": {b"name": b"Movie.2024.1080p.mkv", b"length": 1000}})

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/results/torznab"):
                return httpx.Response(200, content=torznab_xml([{
                    "title": "Movie.2024.1080p", "size": 1000,
                    "link": "http://siteA/dl/1", "attrs": [],
                }]))
            return httpx.Response(200, content=torrent_bytes)

        m = make_matcher(handler, indexers=["siteA"])
        # 本地有两文件,候选只有 mkv(单文件种) → 命中率 0.5 < 0.9
        t = make_torrent_with_files(LOCAL_FILES)
        self.assertEqual(m.match(t, LOCAL_FILES, 10), [])
        # 本地只有 mkv → 命中
        t2 = make_torrent_with_files([("Movie.2024.1080p.mkv", 1000)])
        cands = m.match(t2, [("Movie.2024.1080p.mkv", 1000)], 10)
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
        t = make_torrent_with_files(LOCAL_FILES, size=1000)
        cands = m.match(t, LOCAL_FILES, 10)
        # B 的 infohash 与本地不同 → 需文件比对 → 下载响应为空 → 丢弃;A 大小超容差丢弃
        self.assertEqual(cands, [])

    def test_whitelist_only(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, content=torznab_xml([]))

        m = make_matcher(handler, indexers=["siteB"])
        t = make_torrent_with_files(LOCAL_FILES)
        with unittest.mock.patch("src.reseed.matcher.time.sleep"):
            m.match(t, LOCAL_FILES, 10)
        # 空结果 → 重试 1 次,共 2 次请求;且只请求白名单站点
        self.assertEqual(paths, ["/api/v2.0/indexers/siteB/results/torznab"] * 2)

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


class TestBuildSearchQueries(unittest.TestCase):
    def test_simplify(self):
        from src.reseed.matcher import build_search_queries
        q = build_search_queries(
            "长安三万里.Chang.An.2023.60FPS.2160p.WEB-DL.H265.10bit.DDP5.1-OurTV")
        self.assertEqual(q, ["长安三万里 Chang An 2023", "长安三万里 2023", "Chang An 2023"])

    def test_keep_year(self):
        from src.reseed.matcher import build_search_queries
        q = build_search_queries("Movie.2024.1080p.BluRay.x264-GROUP")
        self.assertEqual(q, ["Movie 2024"])

    def test_no_tags(self):
        from src.reseed.matcher import build_search_queries
        self.assertEqual(build_search_queries("Simple.Movie"), ["Simple Movie"])

    def test_pure_cjk(self):
        from src.reseed.matcher import build_search_queries
        q = build_search_queries("罚罪.第二季.2025.2160p.WEB-DL-ADWeb")
        self.assertEqual(q, ["罚罪 第二季 2025"])

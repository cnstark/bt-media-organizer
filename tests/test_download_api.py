"""POST /api/v1/download 端点单测(用假适配器,不依赖真实下载器网络)。"""
import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.server import _Handler  # noqa: E402


class FakeAdapter:
    """最小 DownloaderAdapter 桩:记录调用,行为可配。"""

    name = "qb"

    def __init__(self):
        self.added = []
        self.present = set()

    def add_torrent(self, data, save_path, *, paused, category="", tags=None,
                    skip_checking=False):
        self.added.append({
            "data": data, "save_path": save_path, "paused": paused,
            "category": category, "tags": tags, "skip_checking": skip_checking,
        })
        # qB 5.x 对 url 与文件上传都会返回带 added_torrent_ids 的 JSON
        return True, json.dumps({"added_torrent_ids": ["abc123"],
                                 "success_count": 1})

    def has_torrent(self, info_hash: str) -> bool:
        return info_hash in self.present

    def list_torrents(self, state: str = "all"):
        return [SimpleNamespace(hash="abc123", name="Test.Movie.2024.1080p",
                                state="downloading", size=12345)]


class FakeEngine:
    def __init__(self, adapter):
        self._adapter = adapter

    def _resolve_adapter(self, downloader=None):
        return self._adapter


class CaptureHandler(_Handler):
    """绕过 HTTP 层,直接捕获 _send 的 (status, payload)。"""

    def __init__(self, adapter):
        self.engine = FakeEngine(adapter)
        self.token = "test-token"
        self.captured = None

    def _send(self, status: int, body: bytes, ctype: str = "application/json"):
        self.captured = (status, json.loads(body))


def make_torrent_bytes() -> bytes:
    """构造最小合法 .torrent(info 字典)字节。"""
    return b"d4:infod4:name5:hello6:lengthi5eeee"


def torrent_hash(data: bytes) -> str:
    from src.downloaders.bencode import info_dict_raw
    return hashlib.sha1(info_dict_raw(data)).hexdigest()


class TestDownloadApi(unittest.TestCase):
    def _post(self, adapter, body: dict):
        h = CaptureHandler(adapter)
        h._download(body)
        return h.captured

    def test_add_by_url(self):
        adapter = FakeAdapter()
        status, payload = self._post(adapter, {
            "downloader": "qb", "url": "http://tracker.example/d.php?id=1&passkey=xx",
            "save_path": "/data/downloads", "category": "movie", "paused": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["data"]["status"], "added")
        self.assertEqual(payload["data"]["downloader"], "qb")
        call = adapter.added[0]
        self.assertEqual(call["data"], "http://tracker.example/d.php?id=1&passkey=xx")
        self.assertEqual(call["save_path"], "/data/downloads")
        self.assertEqual(call["category"], "movie")
        self.assertTrue(call["paused"])
        self.assertEqual(payload["data"]["name"], "Test.Movie.2024.1080p")
        self.assertEqual(payload["data"]["info_hash"], "abc123")

    def test_add_by_torrent_bytes(self):
        adapter = FakeAdapter()
        tb = make_torrent_bytes()
        status, payload = self._post(adapter, {
            "torrent": base64.b64encode(tb).decode("ascii"),
            "save_path": "/data/downloads",
        })
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["data"]["status"], "added")
        call = adapter.added[0]
        self.assertIsInstance(call["data"], bytes)
        self.assertEqual(call["data"], tb)
        self.assertEqual(payload["data"]["info_hash"], torrent_hash(tb))

    def test_already_present(self):
        adapter = FakeAdapter()
        tb = make_torrent_bytes()
        adapter.present.add(torrent_hash(tb))
        status, payload = self._post(adapter, {
            "torrent": base64.b64encode(tb).decode("ascii"),
        })
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["data"]["status"], "already_present")
        self.assertEqual(adapter.added, [])  # 不应重复添加

    def test_missing_source(self):
        status, payload = self._post(FakeAdapter(), {"save_path": "/x"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], 400)

    def test_invalid_url_scheme(self):
        status, payload = self._post(FakeAdapter(), {"url": "ftp://x/y"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], 400)

    def test_add_failure(self):
        class FailAdapter(FakeAdapter):
            def add_torrent(self, data, save_path, **kw):
                return False, "Fails."

        status, payload = self._post(FailAdapter(), {"url": "http://x/y.torrent"})
        self.assertEqual(payload["code"], 1)
        self.assertEqual(payload["data"]["status"], "failed")

    def test_no_adapter(self):
        class NoEngine:
            def _resolve_adapter(self, downloader=None):
                return None

        h = CaptureHandler(FakeAdapter())
        h.engine = NoEngine()
        h._download({"url": "http://x/y.torrent"})
        status, payload = h.captured
        self.assertEqual(payload["code"], 1)
        self.assertIn("下载器不可用", payload["message"])


if __name__ == "__main__":
    unittest.main()

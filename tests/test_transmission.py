"""Transmission 适配器单测(mock httpx 传输层)。"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from src.config import DownloaderConf
from src.downloaders.transmission import TransmissionAdapter


def make_conf(**kw) -> DownloaderConf:
    base = dict(name="tr", type="transmission", url="http://127.0.0.1:9091",
                username="u", password="p")
    base.update(kw)
    return DownloaderConf(**base)


def rpc_response(result="success", arguments=None):
    return httpx.Response(200, json={"result": result, "arguments": arguments or {}})


def make_adapter(handler) -> TransmissionAdapter:
    conf = make_conf()
    adapter = TransmissionAdapter(conf)
    adapter._client = httpx.Client(
        base_url=conf.url.rstrip("/"), transport=httpx.MockTransport(handler)
    )
    return adapter


TORRENT_ITEM = {
    "id": 1, "hashString": "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
    "name": "Test.Movie.2024.1080p", "downloadDir": "/data/downloads",
    "totalSize": 1024, "status": 6, "percentDone": 1.0,
    "labels": ["已整理"], "torrentFile": "/config/torrents/1.torrent",
    "trackers": [{"announce": "http://tr.example/announce"}],
}


class TestTransmission(unittest.TestCase):
    def test_409_session_retry(self):
        """首次 409 取 session-id,重试成功。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(409, headers={"X-Transmission-Session-Id": "SID-123"})
            body = json.loads(request.content)
            self.assertEqual(request.headers["X-Transmission-Session-Id"], "SID-123")
            self.assertEqual(body["method"], "session-get")
            return rpc_response(arguments={"version": "4.0.0"})

        adapter = make_adapter(handler)
        self.assertEqual(adapter.app_version(), "4.0.0")
        self.assertEqual(len(calls), 2)

    def test_list_torrents_seeding_filter(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.assertEqual(body["method"], "torrent-get")
            return rpc_response(arguments={"torrents": [TORRENT_ITEM]})

        adapter = make_adapter(handler)
        all_t = adapter.list_torrents("all")
        self.assertEqual(len(all_t), 1)
        t = all_t[0]
        self.assertEqual(t.hash, TORRENT_ITEM["hashString"].lower())
        self.assertTrue(t.seeding)
        self.assertTrue(t.done)
        self.assertEqual(t.tracker, "http://tr.example/announce")
        self.assertEqual(t.torrent_file, "/config/torrents/1.torrent")
        self.assertEqual(adapter.list_torrents("seeding"), all_t)
        self.assertEqual(adapter.list_finished(), all_t)

    def test_list_torrents_excludes_downloading(self):
        item = dict(TORRENT_ITEM, status=4, percentDone=0.5)  # downloading

        def handler(request):
            return rpc_response(arguments={"torrents": [item]})

        adapter = make_adapter(handler)
        self.assertEqual(len(adapter.list_torrents("seeding")), 0)
        self.assertEqual(len(adapter.list_finished()), 0)
        self.assertEqual(len(adapter.list_torrents("all")), 1)

    def test_add_torrent_metainfo(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return rpc_response()

        adapter = make_adapter(handler)
        ok, msg = adapter.add_torrent(b"TORRENT-BYTES", "/data/media", paused=True,
                                      category="已转移", tags=["t1"])
        self.assertTrue(ok)
        args = captured["body"]["arguments"]
        self.assertEqual(args["download-dir"], "/data/media")
        self.assertTrue(args["paused"])
        self.assertEqual(args["labels"], ["已转移", "t1"])
        self.assertTrue(args["metainfo"].startswith("VE9SUkVOVC1C"))

    def test_add_torrent_url(self):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return rpc_response()

        adapter = make_adapter(handler)
        ok, _ = adapter.add_torrent("http://x/t.torrent", "/data", paused=False)
        self.assertTrue(ok)
        self.assertEqual(captured["body"]["arguments"]["filename"], "http://x/t.torrent")

    def test_delete_and_recheck(self):
        calls = []

        def handler(request):
            calls.append(json.loads(request.content)["method"])
            return rpc_response()

        adapter = make_adapter(handler)
        self.assertTrue(adapter.delete_torrent("HASH", delete_files=False))
        self.assertTrue(adapter.recheck("HASH"))
        self.assertEqual(calls, ["torrent-remove", "torrent-verify"])

    def test_has_torrent(self):
        def handler(request):
            return rpc_response(arguments={"torrents": [TORRENT_ITEM]})

        adapter = make_adapter(handler)
        self.assertTrue(adapter.has_torrent("HASH"))

    def test_get_torrent_file_missing(self):
        def handler(request):
            return rpc_response(arguments={"torrents": [TORRENT_ITEM]})

        adapter = make_adapter(handler)
        with patch("src.downloaders.transmission.Path.read_bytes",
                   side_effect=OSError("no such file")):
            self.assertIsNone(adapter.get_torrent_file("HASH"))


if __name__ == "__main__":
    unittest.main()

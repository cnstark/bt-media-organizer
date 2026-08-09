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


def jsonrpc_response(result=None, error=None):
    """新协议(JSON-RPC 2.0)响应。"""
    body = {"jsonrpc": "2.0", "id": 1}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result or {}
    return httpx.Response(200, json=body)


def probe_aware(handler, version="4.0.0"):
    """包装 handler:自动应答老协议 session-get 探测(其余请求透传)。"""

    def wrapped(request):
        body = json.loads(request.content)
        if "jsonrpc" not in body and body.get("method") == "session-get":
            return rpc_response(arguments={"version": version})
        return handler(request)

    return wrapped


def make_41_adapter(handler) -> TransmissionAdapter:
    """4.1+ 假服务端:自动应答老协议探测 + 新协议 session_get 冒烟。"""

    def wrapped(request):
        body = json.loads(request.content)
        if "jsonrpc" not in body and body.get("method") == "session-get":
            return rpc_response(arguments={"version": "4.1.0"})
        if "jsonrpc" in body and body.get("method") == "session_get":
            return jsonrpc_response({"version": "4.1.0"})
        return handler(request)

    return make_adapter(wrapped)


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

# 新协议(snake_case)响应形态
TORRENT_ITEM_NEW = {
    "id": 1, "hash_string": "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
    "name": "Test.Movie.2024.1080p", "download_dir": "/data/downloads",
    "total_size": 1024, "status": 6, "percent_done": 1.0,
    "labels": ["已整理"], "torrent_file": "/config/torrents/1.torrent",
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

        adapter = make_adapter(probe_aware(handler))
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

        adapter = make_adapter(probe_aware(handler))
        self.assertEqual(len(adapter.list_torrents("seeding")), 0)
        self.assertEqual(len(adapter.list_finished()), 0)
        self.assertEqual(len(adapter.list_torrents("all")), 1)

    def test_add_torrent_metainfo(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return rpc_response()

        adapter = make_adapter(probe_aware(handler))
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

        adapter = make_adapter(probe_aware(handler))
        ok, _ = adapter.add_torrent("http://x/t.torrent", "/data", paused=False)
        self.assertTrue(ok)
        self.assertEqual(captured["body"]["arguments"]["filename"], "http://x/t.torrent")

    def test_delete_and_recheck(self):
        calls = []

        def handler(request):
            calls.append(json.loads(request.content)["method"])
            return rpc_response()

        adapter = make_adapter(probe_aware(handler))
        self.assertTrue(adapter.delete_torrent("HASH", delete_files=False))
        self.assertTrue(adapter.recheck("HASH"))
        self.assertEqual(calls, ["torrent-remove", "torrent-verify"])

    def test_has_torrent(self):
        def handler(request):
            return rpc_response(arguments={"torrents": [TORRENT_ITEM]})

        adapter = make_adapter(probe_aware(handler))
        self.assertTrue(adapter.has_torrent("HASH"))

    def test_get_torrent_file_missing(self):
        def handler(request):
            return rpc_response(arguments={"torrents": [TORRENT_ITEM]})

        adapter = make_adapter(probe_aware(handler))
        with patch("src.downloaders.transmission.Path.read_bytes",
                   side_effect=OSError("no such file")):
            self.assertIsNone(adapter.get_torrent_file("HASH"))

    def test_41_new_protocol_list(self):
        """4.1+ 服务端:探测后走 JSON-RPC 2.0,字段 snake_case。"""
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            self.assertIn("jsonrpc", captured["body"])
            self.assertEqual(captured["body"]["method"], "torrent_get")
            return jsonrpc_response({"torrents": [TORRENT_ITEM_NEW]})

        adapter = make_41_adapter(handler)
        all_t = adapter.list_torrents("all")
        self.assertEqual(len(all_t), 1)
        t = all_t[0]
        self.assertEqual(t.hash, TORRENT_ITEM_NEW["hash_string"].lower())
        self.assertTrue(t.seeding)
        self.assertTrue(t.done)
        self.assertEqual(t.torrent_file, "/config/torrents/1.torrent")
        self.assertEqual(t.tracker, "http://tr.example/announce")
        fields = captured["body"]["params"]["fields"]
        self.assertIn("hash_string", fields)
        self.assertNotIn("hashString", fields)
        self.assertNotIn("downloadDir", fields)

    def test_41_new_protocol_write_ops(self):
        """4.1+:torrent_add/remove/verify 参数 snake_case。"""
        captured = {}

        def handler(request):
            body = json.loads(request.content)
            captured["method"] = body.get("method")
            captured["params"] = body.get("params", {})
            if body.get("method") == "torrent_get":
                return jsonrpc_response({"torrents": [TORRENT_ITEM_NEW]})
            return jsonrpc_response()

        adapter = make_41_adapter(handler)
        ok, _ = adapter.add_torrent(b"BYTES", "/data/media", paused=True,
                                    category="c", tags=["t"])
        self.assertTrue(ok)
        self.assertEqual(captured["method"], "torrent_add")
        self.assertEqual(captured["params"]["download_dir"], "/data/media")
        self.assertEqual(captured["params"]["labels"], ["c", "t"])
        self.assertTrue(captured["params"]["paused"])
        self.assertNotIn("download-dir", captured["params"])

        self.assertTrue(adapter.delete_torrent("HASH", delete_files=True))
        self.assertEqual(captured["method"], "torrent_remove")
        self.assertTrue(captured["params"]["delete_local_data"])
        self.assertNotIn("delete-local-data", captured["params"])

        self.assertTrue(adapter.recheck("HASH"))
        self.assertEqual(captured["method"], "torrent_verify")

    def test_41_fallback_old_protocol_on_400(self):
        """4.1 服务端拒绝新协议(400)→ 自动回退老协议,功能不受影响。"""
        captured = {}

        def handler(request):
            body = json.loads(request.content)
            captured["body"] = body
            if "jsonrpc" in body:  # 新协议一律 400
                return httpx.Response(400, text="Bad Request")
            if body.get("method") == "torrent-get":
                return rpc_response(arguments={"torrents": [TORRENT_ITEM]})
            return rpc_response(arguments={"version": "4.1.0"})

        adapter = make_adapter(handler)
        all_t = adapter.list_torrents("all")
        self.assertEqual(len(all_t), 1)
        self.assertEqual(all_t[0].hash, TORRENT_ITEM["hashString"].lower())
        # 回退后实际使用的是老协议请求
        self.assertIn("arguments", captured["body"])
        self.assertEqual(captured["body"]["method"], "torrent-get")


if __name__ == "__main__":
    unittest.main()

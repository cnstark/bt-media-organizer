"""转移引擎单测(用 mock 适配器)。"""
import unittest
from pathlib import Path

from src.config import TransferConf, PathRuleConf
from src.downloaders.base import DownloaderAdapter, TorrentInfo
from src.downloaders.bencode import encode
from src.transfer.engine import TransferEngine


def make_torrent(hash_: str = "h1", save_path: str = "/downloads/movie") -> TorrentInfo:
    return TorrentInfo(hash=hash_, name=f"name-{hash_}", save_path=Path(save_path),
                       content_path=Path(save_path), size=100, seeding=True,
                       state="stalledUP")


class FakeAdapter(DownloaderAdapter):
    """记录调用的假适配器。"""

    def __init__(self, name="qb", torrents=None, have=None, torrent_path=""):
        self.name = name
        self.conf = type("C", (), {"torrent_path": torrent_path})()
        self.torrents = torrents or []
        self.have = set(have or [])
        self.added = []
        self.deleted = []
        self.checked = []

    # 整理接口
    def list_finished(self):
        return [t for t in self.torrents if t.done]

    def add_tag(self, hash_):
        return True

    def delete_torrent(self, hash_, delete_files=True):
        self.deleted.append((hash_, delete_files))
        return True

    # v2 接口
    def list_torrents(self, state="all"):
        if state == "seeding":
            return [t for t in self.torrents if t.seeding]
        if state == "completed":
            return [t for t in self.torrents if t.done]
        return list(self.torrents)

    def get_torrent_file(self, hash_):
        data = encode({b"info": {b"name": b"x"}})
        return data if hash_ in {t.hash for t in self.torrents} else None

    def add_torrent(self, data, save_path, *, paused, category="", tags=None, skip_checking=False):
        self.added.append({"data": data, "save_path": save_path, "paused": paused,
                           "category": category, "tags": tags})
        return True, "ok"

    def recheck(self, hash_):
        self.checked.append(hash_)
        return True

    def app_version(self):
        return "4.5.0"

    def has_torrent(self, hash_):
        return hash_ in self.have


def make_conf(**kw) -> TransferConf:
    base = dict(enabled=True, from_client="qb", to_client="tr", auto_start=True,
                delete_source=False, marker="empty",
                path=PathRuleConf(convert_type="eq", rules=[], filter_paths=[], selector_paths=[]))
    base.update(kw)
    return TransferConf(**base)


class TestTransferEngine(unittest.TestCase):
    def test_transfer_success(self):
        src = FakeAdapter(torrents=[make_torrent("h1")])
        dst = FakeAdapter()
        engine = TransferEngine(make_conf(), src, dst)
        ok, msg = engine.transfer_one(src.torrents[0])
        self.assertTrue(ok)
        self.assertEqual(len(dst.added), 1)
        self.assertEqual(dst.added[0]["save_path"], "/downloads/movie")
        self.assertFalse(dst.added[0]["paused"])  # auto_start=True

    def test_skip_when_target_has(self):
        src = FakeAdapter(torrents=[make_torrent("h1")])
        dst = FakeAdapter(have=["h1"])
        engine = TransferEngine(make_conf(), src, dst)
        ok, msg = engine.transfer_one(src.torrents[0])
        self.assertFalse(ok)
        self.assertEqual(msg, "skipped")
        self.assertEqual(dst.added, [])

    def test_paused_when_auto_start_false(self):
        src = FakeAdapter(torrents=[make_torrent("h1")])
        dst = FakeAdapter()
        engine = TransferEngine(make_conf(auto_start=False), src, dst)
        ok, _ = engine.transfer_one(src.torrents[0])
        self.assertTrue(ok)
        self.assertTrue(dst.added[0]["paused"])

    def test_delete_source(self):
        src = FakeAdapter(torrents=[make_torrent("h1")])
        dst = FakeAdapter()
        engine = TransferEngine(make_conf(delete_source=True), src, dst)
        ok, _ = engine.transfer_one(src.torrents[0])
        self.assertTrue(ok)
        self.assertEqual(src.deleted, [("h1", False)])  # 只删种子不删数据

    def test_marker_category(self):
        src = FakeAdapter(torrents=[make_torrent("h1")])
        dst = FakeAdapter()
        engine = TransferEngine(make_conf(marker="category"), src, dst)
        engine.transfer_one(src.torrents[0])
        self.assertEqual(dst.added[0]["category"], "已转移")

    def test_marker_tag(self):
        src = FakeAdapter(torrents=[make_torrent("h1")])
        dst = FakeAdapter()
        engine = TransferEngine(make_conf(marker="tag"), src, dst)
        engine.transfer_one(src.torrents[0])
        self.assertEqual(dst.added[0]["tags"], ["已转移"])

    def test_path_convert_replace(self):
        src = FakeAdapter(torrents=[make_torrent("h1", save_path="/downloads/movie")])
        dst = FakeAdapter()
        engine = TransferEngine(make_conf(path=PathRuleConf(
            convert_type="replace", rules=[("/downloads", "/volume1/downloads")])), src, dst)
        ok, _ = engine.transfer_one(src.torrents[0])
        self.assertTrue(ok)
        self.assertEqual(dst.added[0]["save_path"], "/volume1/downloads/movie")

    def test_path_filter_excluded(self):
        src = FakeAdapter(torrents=[make_torrent("h1", save_path="/downloads/tmp/x")])
        dst = FakeAdapter()
        engine = TransferEngine(make_conf(path=PathRuleConf(filter_paths=["/downloads/tmp"])), src, dst)
        ok, msg = engine.transfer_one(src.torrents[0])
        self.assertFalse(ok)
        self.assertEqual(msg, "skipped")

    def test_convert_fail(self):
        src = FakeAdapter(torrents=[make_torrent("h1", save_path="/other/x")])
        dst = FakeAdapter()
        engine = TransferEngine(make_conf(path=PathRuleConf(
            convert_type="replace", rules=[("/downloads", "/volume1")])), src, dst)
        ok, msg = engine.transfer_one(src.torrents[0])
        self.assertFalse(ok)
        self.assertIn("路径转换失败", msg)

    def test_run_once_stats(self):
        src = FakeAdapter(torrents=[
            make_torrent("h1", save_path="/downloads/a"),
            make_torrent("h2", save_path="/downloads/b"),
        ])
        dst = FakeAdapter(have=["h2"])
        engine = TransferEngine(make_conf(), src, dst)
        stats = engine.run_once()
        self.assertEqual(stats, {"total": 2, "transferred": 1, "skipped": 1, "failed": 0})
        self.assertIsNotNone(engine.last_run)


if __name__ == "__main__":
    unittest.main()

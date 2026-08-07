"""辅种引擎单测(mock 适配器 + 假匹配器 + 临时 SQLite)。"""
import os
import tempfile
import unittest
from pathlib import Path

from src.config import JackettConf, ReseedConf
from src.downloaders.base import TorrentInfo
from src.reseed.engine import ReseedEngine
from src.reseed.matcher import Candidate, Matcher
from src.reseed.store import ReseedStore

H1 = "a" * 40


def make_torrent(hash_: str, save_path: str = "/data/tv") -> TorrentInfo:
    return TorrentInfo(hash=hash_, name=f"n-{hash_[:6]}", save_path=Path(save_path),
                       content_path=Path(save_path), size=1000, seeding=True)


class FakeAdapter:
    """假下载器:有做种列表,可注入。"""

    def __init__(self, name="qb", torrents=None, have=None, fail_inject=False):
        self.name = name
        self.conf = type("C", (), {"torrent_path": ""})()
        self.torrents = torrents or []
        self.have = set(have or [])
        self.added = []
        self.checked = []
        self.fail_inject = fail_inject

    def list_torrents(self, state="all"):
        if state == "seeding":
            return [t for t in self.torrents if t.seeding]
        return list(self.torrents)

    def has_torrent(self, hash_):
        return hash_ in self.have

    def add_torrent(self, data, save_path, *, paused, category="", tags=None, skip_checking=False):
        if self.fail_inject:
            return False, "注入失败(测试)"
        self.added.append({"save_path": save_path, "paused": paused,
                           "category": category, "tags": tags})
        return True, "ok"

    def recheck(self, hash_):
        self.checked.append(hash_)
        return True


class FakeMatcher(Matcher):
    """固定候选;可配置下载失败。"""

    def __init__(self, candidates=None, fail_download=False):
        self.candidates = candidates or []
        self.fail_download = fail_download

    def match(self, torrent, candidates_limit):
        return list(self.candidates)[:candidates_limit]

    def download(self, url):
        if self.fail_download:
            raise RuntimeError("下载失败(测试)")
        return b"TORRENT"


def make_conf(**kw) -> ReseedConf:
    base = dict(enabled=True, target_client="qb", auto_start=False,
                marker="category",
                jackett=JackettConf(url="http://j", api_key="k",
                                    indexers=["siteA"], per_indexer_delay=0))
    base.update(kw)
    return ReseedConf(**base)


def make_store() -> ReseedStore:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return ReseedStore(path), path


def make_cand(info_hash: str = "") -> Candidate:
    return Candidate(indexer="siteA", torrent_id="t1", title="x",
                     size=1000, download_url="http://siteA/dl/1", info_hash=info_hash)


class TestReseedEngine(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = make_store()

    def tearDown(self):
        self.store.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _engine(self, target, src, matcher, conf=None):
        """target_client='qb' 指向 target;源种子放在 'tr'。"""
        return ReseedEngine(conf or make_conf(), {"qb": target, "tr": src},
                            self.store, matcher)

    def test_match_and_inject(self):
        """新种子匹配 → 注入成功(默认暂停 + category 标记)。"""
        src = FakeAdapter(name="tr", torrents=[make_torrent(H1)])
        target = FakeAdapter(name="qb")
        engine = self._engine(target, src, FakeMatcher([make_cand()]))
        stats = engine.run_once()
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["injected"], 1)
        self.assertEqual(len(target.added), 1)
        self.assertTrue(target.added[0]["paused"])            # auto_start=False 默认暂停
        self.assertEqual(target.added[0]["save_path"], "/data/tv")
        self.assertEqual(target.added[0]["category"], "辅种")  # marker=category
        rows = self.store.list(status="success")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].site, "siteA")
        self.assertEqual(rows[0].source_hash, H1)

    def test_idempotent_second_run(self):
        src = FakeAdapter(name="tr", torrents=[make_torrent(H1)])
        target = FakeAdapter(name="qb")
        engine = self._engine(target, src, FakeMatcher([make_cand()]))
        engine.run_once()
        engine.run_once()
        self.assertEqual(len(target.added), 1)      # 不重复注入
        self.assertEqual(len(self.store.list()), 1)  # 不重复入队

    def test_failed_retry_next_round(self):
        src = FakeAdapter(name="tr", torrents=[make_torrent(H1)])
        target = FakeAdapter(name="qb", fail_inject=True)
        engine = self._engine(target, src, FakeMatcher([make_cand()]))
        stats = engine.run_once()
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(len(self.store.list(status="failed")), 1)
        # 下轮:目标恢复 → 自动重试成功
        target.fail_inject = False
        stats2 = engine.run_once()
        self.assertEqual(stats2["injected"], 1)
        self.assertEqual(len(self.store.list(status="success")), 1)

    def test_target_has_source_hash_skipped(self):
        src = FakeAdapter(name="tr", torrents=[make_torrent(H1)])
        target = FakeAdapter(name="qb", have={H1})
        engine = self._engine(target, src, FakeMatcher([]))
        stats = engine.run_once()
        self.assertEqual(stats["skipped"], 1)
        rows = self.store.list(status="skipped")
        self.assertEqual(len(rows), 1)
        self.assertIn("已有", rows[0].message)

    def test_exclude_paths(self):
        src = FakeAdapter(name="tr", torrents=[make_torrent(H1, save_path="/data/tmp/x")])
        target = FakeAdapter(name="qb")
        engine = self._engine(target, src, FakeMatcher([make_cand()]),
                              conf=make_conf(exclude_paths=["/data/tmp"]))
        stats = engine.run_once()
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(target.added, [])

    def test_redo_failed(self):
        src = FakeAdapter(name="tr", torrents=[make_torrent(H1)])
        target = FakeAdapter(name="qb", fail_inject=True)
        engine = self._engine(target, src, FakeMatcher([make_cand()]))
        engine.run_once()
        row = self.store.list(status="failed")[0]
        target.fail_inject = False
        ok, msg = engine.redo(row.id)
        self.assertTrue(ok)
        self.assertEqual(self.store.get(row.id).status, "success")

    def test_download_fail_marks_failed(self):
        src = FakeAdapter(name="tr", torrents=[make_torrent(H1)])
        target = FakeAdapter(name="qb")
        engine = self._engine(target, src, FakeMatcher([make_cand()], fail_download=True))
        stats = engine.run_once()
        self.assertEqual(stats["failed"], 1)
        rows = self.store.list(status="failed")
        self.assertEqual(len(rows), 1)
        self.assertIn("下载种子失败", rows[0].message)


if __name__ == "__main__":
    unittest.main()

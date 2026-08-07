"""bencode 工具单测。"""
import hashlib
import unittest

from src.downloaders.bencode import (
    decode, encode, extract_announce, fastresume_tracker, file_list, info_dict_raw,
    info_hash, patch_announce,
)


def make_torrent(announce: str = "http://tracker.example/announce", name: str = "test.mkv") -> bytes:
    """构造最小 .torrent。"""
    info = {
        b"name": name.encode(),
        b"piece length": 16384,
        b"pieces": b"\x00" * 20,
        b"length": 1000,
    }
    d = {b"announce": announce.encode(), b"info": info}
    return encode(d)


class TestBencode(unittest.TestCase):
    def test_roundtrip(self):
        obj = {b"a": 1, b"b": [b"x", {b"c": b"y"}], b"n": -5}
        self.assertEqual(decode(encode(obj)), obj)

    def test_decode_simple(self):
        self.assertEqual(decode(b"i42e"), 42)
        self.assertEqual(decode(b"4:spam"), b"spam")
        self.assertEqual(decode(b"l4:spami42ee"), [b"spam", 42])
        self.assertEqual(decode(b"d3:cow3:moo4:spam4:eggse"), {b"cow": b"moo", b"spam": b"eggs"})

    def test_info_hash(self):
        data = make_torrent()
        raw = info_dict_raw(data)
        self.assertEqual(info_hash(data), hashlib.sha1(raw).hexdigest())
        # 与重编码不一致时也必须用原始切片(模拟非规范顺序的 info 键)
        weird = encode({b"zz": 1, b"info": {b"b": 2, b"a": 1}})
        self.assertEqual(
            info_hash(weird),
            hashlib.sha1(info_dict_raw(weird)).hexdigest(),
        )

    def test_extract_announce(self):
        self.assertEqual(extract_announce(make_torrent()), "http://tracker.example/announce")
        self.assertIsNone(extract_announce(encode({b"info": {}})))

    def test_patch_announce(self):
        # 已有 announce → 原样返回
        data = make_torrent()
        self.assertEqual(patch_announce(data, "http://new.example/ann"), data)
        # 无 announce → 补上
        bare = encode({b"info": {b"name": b"x"}})
        patched = patch_announce(bare, "http://new.example/ann")
        self.assertEqual(extract_announce(patched), "http://new.example/ann")

    def test_fastresume_tracker(self):
        fr = encode({b"trackers": [[b"http://a.example/ann", b"http://b.example/ann"]]})
        self.assertEqual(fastresume_tracker(fr), "http://a.example/ann")
        self.assertIsNone(fastresume_tracker(encode({b"trackers": []})))
        self.assertIsNone(fastresume_tracker(b"not bencode"))

    def test_file_list_single(self):
        data = encode({b"info": {b"name": b"Movie.mkv", b"length": 100, b"piece length": 1}})
        self.assertEqual(file_list(data), [("Movie.mkv", 100)])

    def test_file_list_multi(self):
        info = {
            b"name": b"Show", b"piece length": 1,
            b"files": [
                {b"length": 10, b"path": [b"Show", b"S01E01.mkv"]},
                {b"length": 5, b"path": [b"Show", b"Subs", b"S01E01.zh.srt"]},
            ],
        }
        data = encode({b"info": info})
        self.assertEqual(file_list(data), [("Show/S01E01.mkv", 10), ("Show/Subs/S01E01.zh.srt", 5)])

    def test_file_list_invalid(self):
        self.assertEqual(file_list(b"not bencode"), [])
        self.assertEqual(file_list(encode({b"info": {}})), [])


if __name__ == "__main__":
    unittest.main()

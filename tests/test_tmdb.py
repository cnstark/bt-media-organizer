"""TMDB 识别逻辑测试(离线,模拟 API 响应)。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import TmdbConf  # noqa: E402
from src.history import HistoryStore  # noqa: E402
from src.parse.filename import ParsedMeta  # noqa: E402
from src.recognize.tmdb import TmdbRecognizer  # noqa: E402

_SEARCH_EN = {
    "results": [{
        "id": 207066, "name": "Brush Up Life", "original_name": "ブラッシュアップライフ",
        "first_air_date": "2023-01-08", "genre_ids": [18],
    }],
}
_TRANSLATIONS = {
    "translations": [
        {"iso_639_1": "ja", "iso_3166_1": "JP", "data": {"name": "ブラッシュアップライフ"}},
        {"iso_639_1": "zh", "iso_3166_1": "CN", "data": {"name": "重启人生", "title": "重启人生"}},
        {"iso_639_1": "zh", "iso_3166_1": "TW", "data": {"name": "重啟人生"}},
    ],
}
_SEARCH_ZH_ALREADY = {
    "results": [{
        "id": 207066, "name": "重启人生", "original_name": "ブラッシュアップライフ",
        "first_air_date": "2023-01-08", "genre_ids": [18],
    }],
}


class _FakeClient:
    """模拟 httpx.Client:按 URL 返回预设响应。"""

    def __init__(self, search_resp, translations_resp=None, fail=False):
        self._search = search_resp
        self._translations = translations_resp
        self._fail = fail
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params or {}))
        if self._fail:
            import httpx
            raise httpx.ConnectTimeout("mock timeout")
        payload = self._translations if "/translations" in url else self._search

        class _R:
            def raise_for_status(self): pass
            def json(self): return payload
        return _R()

    def close(self):
        pass


def _recognizer(client, language="zh-CN"):
    conf = TmdbConf(enabled=True, api_key="fake", language=language)
    with tempfile.TemporaryDirectory() as tmp:
        store = HistoryStore(str(Path(tmp) / "t.db"))
        rec = TmdbRecognizer(conf, store)
        rec._client = client  # 注入 fake
        return rec, store


def test_localized_title_via_translations():
    """search 返回英文名,language=zh-CN 时应通过 translations 拿到中文名。"""
    rec, store = _recognizer(_FakeClient(_SEARCH_EN, _TRANSLATIONS))
    meta = ParsedMeta(title="Brush Up Life", year=2023)
    m = rec.recognize(meta)
    assert m is not None, "识别不应为空"
    assert m.title == "重启人生", m.title
    assert m.year == 2023
    assert m.tmdb_id == 207066
    # 第二次调用走缓存,不再请求
    calls = len(rec._client.calls)
    rec.recognize(meta)
    assert len(rec._client.calls) == calls, "应命中缓存"
    store.close()


def test_localized_title_already_chinese():
    """search 直接返回中文名时不再请求 translations。"""
    rec, store = _recognizer(_FakeClient(_SEARCH_ZH_ALREADY, _TRANSLATIONS))
    m = rec.recognize(ParsedMeta(title="Brush Up Life", year=2023))
    assert m.title == "重启人生", m.title
    assert not any("/translations" in u for u, _ in rec._client.calls)
    store.close()


def test_network_failure_falls_back_none():
    """网络失败返回 None(上层回退文件名解析),不抛异常。"""
    rec, store = _recognizer(_FakeClient(_SEARCH_EN, _TRANSLATIONS, fail=True))
    m = rec.recognize(ParsedMeta(title="Some.Show", year=2023))
    assert m is None
    store.close()


def test_cache_key_contains_language():
    rec, store = _recognizer(_FakeClient(_SEARCH_EN, _TRANSLATIONS))
    meta = ParsedMeta(title="Brush Up Life", year=2023, season=1)  # 带季 → tv
    rec.recognize(meta)
    assert store.cache_get("tv|zh-CN|Brush Up Life|2023"), "缓存键应含语言"
    store.close()



class _RoutingClient:
    """按查询词路由:CJK 查询返回空,英文查询返回结果。"""

    def __init__(self, zh_results, en_results):
        self._zh, self._en = zh_results, en_results
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params or {}))
        payload = self._en if params.get("query") and not _HAS_CJK.search(params["query"]) else self._zh
        class _R:
            def raise_for_status(self): pass
            def json(self): return {"results": payload}
        return _R()

    def close(self):
        pass


import re as _re
_HAS_CJK = _re.compile(r"[\u4e00-\u9fff]")

_EN_RESULT = [{
    "id": 60781, "name": "舌尖上的中国", "original_name": "A Bite of China",
    "first_air_date": "2012-05-14", "genre_ids": [99], "origin_country": ["CN"],
    "original_language": "zh",
}]


def test_latin_fallback_query():
    """中文+英文混合标题:中文查询无结果时用纯英文标题兜底搜索。"""
    rec, store = _recognizer(_RoutingClient([], _EN_RESULT))
    meta = ParsedMeta(title="中央广播电视总台4K超高清频道 舌尖上的中国 CCTV-4K A Bite of China",
                      year=2025, season=4,
                      tokens=["中央广播电视总台4K超高清频道", "舌尖上的中国", "CCTV-4K", "A", "Bite", "of", "China"])
    m = rec.recognize(meta)
    assert m is not None
    assert m.title == "舌尖上的中国", m.title
    assert m.tmdb_id == 60781
    # 第二次查询应包含英文标题
    queries = [params.get("query") for _, params in rec._client.calls]
    assert any("A Bite of China" in q for q in queries), queries
    store.close()

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)

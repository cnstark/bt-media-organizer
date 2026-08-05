"""TMDB 媒体识别(可选增强,失败回退文件名解析结果)。

识别流程:按标题+年份搜索 movie/tv → 取最佳匹配 → 中文标题增强
(translations 兜底)→ 缓存(SQLite,键含语言)。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

from ..config import TmdbConf
from ..history import HistoryStore
from ..parse.filename import ParsedMeta

logger = logging.getLogger("lite-organizer.tmdb")

# 是否包含 CJK 字符(判断标题是否为中文)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class MediaInfo:
    """识别结果(供命名模板与类别规则使用的规范字段)。"""

    title: str = ""
    original_title: str = ""
    year: Optional[int] = None
    media_type: str = ""          # movie / tv
    tmdb_id: Optional[int] = None
    original_language: str = ""   # 原始语种(如 zh/en/ja)
    genre_ids: List[int] = field(default_factory=list)   # 类型 ID 列表
    origin_country: List[str] = field(default_factory=list)   # 出品国家/地区(剧集)
    production_countries: List[str] = field(default_factory=list)  # 制片国家/地区(电影,需详情接口)
    overview: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "original_title": self.original_title,
            "year": self.year,
            "media_type": self.media_type,
            "tmdb_id": self.tmdb_id,
            "original_language": self.original_language,
            "genre_ids": self.genre_ids,
            "origin_country": self.origin_country,
            "production_countries": self.production_countries,
            "overview": self.overview,
        }


class TmdbRecognizer:
    """TMDB 识别器;enabled=False 时 recognize() 直接返回 None。"""

    def __init__(self, conf: TmdbConf, store: HistoryStore):
        self.conf = conf
        self.store = store
        self._client = None
        if conf.enabled:
            self._client = httpx.Client(timeout=conf.timeout, proxy=conf.proxy or None)
            logger.info(f"TMDB 识别已启用,API: {conf.api_base},语言: {conf.language}")

    def close(self):
        if self._client:
            self._client.close()

    @property
    def enabled(self) -> bool:
        return self.conf.enabled

    def recognize(self, meta: ParsedMeta) -> Optional[MediaInfo]:
        """按解析出的标题+年份识别;命中返回 MediaInfo,否则 None。"""
        if not self.enabled or not self._client:
            return None
        if not meta.title:
            return None
        media_type = "tv" if meta.is_tv else "movie"
        # 缓存键含语言,避免切换语言后命中旧结果
        key = f"{media_type}|{self.conf.language}|{meta.title}|{meta.year or ''}"

        cached = self.store.cache_get(key)
        if cached is not None:
            return MediaInfo(**cached) if cached else None

        result = self._search(media_type, meta.title, meta.year)
        if result:
            self.store.cache_set(key, result.to_dict())
            logger.info(f"TMDB 识别: {meta.title} -> {result.title} ({result.year})"
                        f"[id={result.tmdb_id} {result.media_type}]")
        else:
            # 缓存 None 结果,避免反复请求未命中
            self.store.cache_set(key, {})
        return result

    # ---------------- 私有 ----------------

    def _get(self, path: str, **params) -> Optional[dict | list]:
        """请求 TMDB,失败返回 None 并记日志(网络不通时明确可见)。"""
        try:
            resp = self._client.get(
                f"{self.conf.api_base.rstrip('/')}{path}",
                params={"api_key": self.conf.api_key, "language": self.conf.language, **params},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.warning(f"TMDB 请求失败 {path}: {e}(检查网络/镜像/api_base)")
            return None
        except ValueError as e:
            logger.warning(f"TMDB 响应解析失败 {path}: {e}")
            return None

    def _search(self, media_type: str, title: str, year: Optional[int]) -> Optional[MediaInfo]:
        data = self._get(f"/search/{media_type}", query=title, year=year or "", include_adult="false")
        results = (data or {}).get("results") or []
        if not results:
            return None

        best = self._pick_best(results, title, year)
        if best is None:
            return None

        # 语言为中文时,优先取本地化标题(translations 兜底)
        name = best.get("name") or best.get("title") or title
        if self.conf.language.lower().startswith("zh") and not _CJK_RE.search(name):
            localized = self._localized_title(media_type, best.get("id"))
            if localized:
                name = localized

        return MediaInfo(
            title=name,
            original_title=best.get("original_name") or best.get("original_title") or "",
            year=_extract_year(best),
            media_type=media_type,
            tmdb_id=best.get("id"),
            original_language=best.get("original_language") or "",
            genre_ids=list(best.get("genre_ids") or []),
            origin_country=list(best.get("origin_country") or []),
            production_countries=list(best.get("production_countries") or []),
            overview=best.get("overview") or "",
        )

    def _localized_title(self, media_type: str, tmdb_id) -> Optional[str]:
        """从 translations 接口取中文标题(zh-CN 优先,zh-TW 兜底)。"""
        if not tmdb_id:
            return None
        data = self._get(f"/{media_type}/{tmdb_id}/translations")
        translations = (data or {}).get("translations") or []
        for iso in ("zh-CN", "zh-TW", "zh"):
            for t in translations:
                if (t.get("iso_639_1") + "-" + (t.get("iso_3166_1") or "")) == iso \
                        or (iso == "zh" and t.get("iso_639_1") == "zh"):
                    name = ((t.get("data") or {}).get("name") or (t.get("data") or {}).get("title") or "").strip()
                    if name:
                        return name
        return None

    @staticmethod
    def _pick_best(results: list, title: str, year: Optional[int]):
        """优先精确年份匹配,其次标题完全一致(忽略大小写)。"""
        t = title.strip().lower()
        exact_year = [r for r in results if year and _extract_year(r) == year]
        candidates = exact_year or results
        for r in candidates:
            name = (r.get("name") or r.get("title") or "").strip().lower()
            if name == t:
                return r
        return candidates[0]


def _extract_year(item: dict) -> Optional[int]:
    date = item.get("release_date") or item.get("first_air_date") or ""
    if len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None

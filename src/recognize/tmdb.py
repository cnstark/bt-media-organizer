"""TMDB 媒体识别(可选增强,失败回退文件名解析结果)。

识别流程:按标题+年份搜索 movie/tv → 取最佳匹配 → 缓存(SQLite)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from ..config import TmdbConf
from ..history import HistoryStore
from ..parse.filename import ParsedMeta

logger = logging.getLogger("lite-organizer.tmdb")

# 电影类别(中文归类,用于配置 category 场景,可自行扩展)
_MOVIE_CATEGORY_MAP = {
    28: "动作", 12: "冒险", 16: "动画", 35: "喜剧", 80: "犯罪",
    18: "剧情", 10751: "家庭", 14: "奇幻", 36: "历史", 27: "恐怖",
    10402: "音乐", 9648: "悬疑", 10749: "爱情", 878: "科幻",
    10770: "电视电影", 53: "惊悚", 10752: "战争", 37: "西部",
}
_TV_CATEGORY_MAP = {
    10759: "动作冒险", 16: "动画", 35: "喜剧", 80: "犯罪", 99: "纪录",
    18: "剧情", 10751: "家庭", 10762: "儿童", 9648: "悬疑", 10765: "科幻奇幻",
    10764: "真人秀", 10766: "肥皂剧", 10767: "脱口秀", 10768: "战争政治", 37: "西部",
}


@dataclass
class MediaInfo:
    """识别结果(供命名模板使用的规范字段)。"""

    title: str = ""
    original_title: str = ""
    year: Optional[int] = None
    media_type: str = ""          # movie / tv
    tmdb_id: Optional[int] = None
    category: Optional[str] = None  # 类别(如"科幻")
    overview: str = ""


class TmdbRecognizer:
    """TMDB 识别器;enabled=False 时 recognize() 直接返回 None。"""

    def __init__(self, conf: TmdbConf, store: HistoryStore):
        self.conf = conf
        self.store = store
        self._client = None
        if conf.enabled:
            self._client = httpx.Client(timeout=conf.timeout)
            logger.info("TMDB 识别已启用")

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
        key = f"{media_type}|{meta.title}|{meta.year or ''}"

        cached = self.store.cache_get(key)
        if cached is not None:
            return MediaInfo(**cached) if cached else None

        result = self._search(media_type, meta.title, meta.year)
        if result:
            self.store.cache_set(key, result.to_dict())
        else:
            # 缓存 None 结果(30 天),避免反复请求未命中
            self.store.cache_set(key, {})
        return result

    # ---------------- 私有 ----------------

    def _search(self, media_type: str, title: str, year: Optional[int]) -> Optional[MediaInfo]:
        try:
            resp = self._client.get(
                f"https://api.themoviedb.org/3/search/{media_type}",
                params={
                    "api_key": self.conf.api_key,
                    "query": title,
                    "language": self.conf.language,
                    "year": year or "",
                    "include_adult": "false",
                },
            )
            resp.raise_for_status()
            results = (resp.json() or {}).get("results") or []
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"TMDB 搜索失败 [{title}]: {e}")
            return None

        if not results:
            return None

        best = self._pick_best(results, title, year)
        if best is None:
            return None

        return MediaInfo(
            title=best.get("name") or best.get("title") or title,
            original_title=best.get("original_name") or best.get("original_title") or "",
            year=_extract_year(best),
            media_type=media_type,
            tmdb_id=best.get("id"),
            category=_category_of(media_type, best.get("genre_ids") or []),
            overview=best.get("overview") or "",
        )

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

    @staticmethod
    def _category_of(media_type: str, genre_ids: list) -> Optional[str]:
        mapping = _TV_CATEGORY_MAP if media_type == "tv" else _MOVIE_CATEGORY_MAP
        for gid in genre_ids:
            if gid in mapping:
                return mapping[gid]
        return None


def _extract_year(item: dict) -> Optional[int]:
    date = item.get("release_date") or item.get("first_air_date") or ""
    if len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None

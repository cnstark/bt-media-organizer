"""辅种匹配器:Matcher 接口 + Jackett 实现(Torznab 搜索 → 大小容差 → 文件级匹配)。

匹配模型(IYUU 实证):不同 PT 站对同一发布重新打包, infohash 不同但文件列表一致。
因此不做 infohash 精确比对,而是下载候选种子解析文件列表,与本地种子文件列表
做同名同大小文件占比比对(≥ 阈值即命中),注入后下载器校验共存做种。
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import httpx

from ..config import JackettConf
from ..downloaders.base import TorrentInfo
from ..downloaders.bencode import file_list

logger = logging.getLogger("bt-media-organizer.reseed.matcher")

_TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"

# 文件级匹配阈值:本地文件命中比例 ≥ 该值视为同源(容忍候选多出 sample/nfo 等)
MATCH_RATIO_THRESHOLD = 0.9

# 每索引器文件比对下载预算:超过则放弃该索引器(候选下载是主要耗时,实测单站可达数十秒)
MAX_DOWNLOAD_PER_INDEXER = 3
# 搜索空结果重试间隔(秒)
RETRY_SLEEP = 3

# tracker 域名关键词 → Jackett 索引器 id(辅种跳过已覆盖站点用; 可用配置覆盖)
DEFAULT_TRACKER_MAP = {
    "btschool": "btschool",
    "hddolby": "hddolby",
    "hdarea": "hdarea",
    "hdfans": "hdfans",
    "m-team": "mteamtp",
    "mteamtp": "mteamtp",
}

# 种子名中的标签 token(搜索词精简时剔除;组名在 '-' 之后一并截掉)
_TAG_TOKENS = {
    "2160p", "1080p", "720p", "4k", "uhd", "60fps", "120fps", "web", "web-dl",
    "webdl", "bluray", "blu-ray", "remux", "hdrip", "bdrip", "webrip", "hdr",
    "hdr10", "hdr10+", "dv", "dolby", "vision", "hevc", "h265", "h264", "x265",
    "x264", "10bit", "8bit", "ddp5.1", "ddp", "ac3", "aac", "flac", "dts",
    "dts-hd", "truehd", "atmos", "multi", "2audio", "3audio", "imax", "hq",
    "repack", "proper", "extended", "remastered", "collection", "complete",
    "cmct", "diy", "dsnp", "nfweb", "fhd", "uhd", "2160", "1080", "sdr",
}
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _has_cjk(token: str) -> bool:
    return bool(_CJK_RE.search(token))


def build_search_queries(name: str) -> List[str]:
    """由完整种子名生成搜索词候选(精简标题, 多词回退)。

    策略: 截掉 '-' 后的组名 → 剔除标签 token → 保留标题 token + 年份。
    站间搜索能力差异(实证):
      - btschool 类: 中英混合词可搜('长安三万里 Chang An 2023' → 23 条)
      - hdarea 类(NexusPHP): 混合词返回 0, 纯中文/纯英文各自可搜
    因此生成 [混合词, 中文词, 英文词] 依次回退。
    例: '长安三万里.Chang.An.2023.60FPS.2160p.WEB-DL.H265.10bit.DDP5.1-OurTV'
        → ['长安三万里 Chang An 2023', '长安三万里 2023', 'Chang An 2023']
    """
    base = name.split("-")[0] if "-" in name else name
    tokens: List[str] = []
    for tok in base.replace(".", " ").split():
        if tok.lower().strip() in _TAG_TOKENS:
            continue
        tokens.append(tok)
    if not tokens:
        return [name]
    m = _YEAR_RE.search(name)
    year = m.group(0) if m else ""
    cjk = [t for t in tokens if _has_cjk(t)]
    # 拉丁 token 排除纯数字(年份已单独处理),避免生成无意义搜索词
    lat = [t for t in tokens if not _has_cjk(t) and not t.isdigit()]
    if not cjk or not lat:
        # 纯中文或纯英文标题:单一搜索词即可
        query = " ".join(tokens)
        if year and year not in query:
            query = f"{query} {year}"
        return [query]
    # 中英混合标题:依次回退 混合 → 纯中文 → 纯英文
    queries: List[str] = []
    for parts in (cjk + lat, cjk, lat):
        query = " ".join(parts)
        if year and year not in query:
            query = f"{query} {year}"
        if query not in queries:
            queries.append(query)
    return queries


@dataclass
class Candidate:
    """匹配到的可辅种候选。"""

    indexer: str          # Jackett 索引器 id
    torrent_id: str       # 结果标识(无稳定 id 时用 link)
    title: str
    size: int
    download_url: str
    info_hash: str = ""   # Torznab 直接返回时非空(仅用于同 hash 快速命中/幂等)
    seeders: int = 0


def match_ratio(local_files: List[Tuple[str, int]], cand_files: List[Tuple[str, int]]) -> float:
    """文件级匹配度:本地文件在候选中的同名(忽略目录)同大小占比(0~1)。

    实证:跨站重新打包的同源种子, 文件名与大小完全一致, 差异主要在目录结构
    (如本地 'OurTV/x.mp4' vs 候选 'x.mp4'), 因此按 basename+size 匹配。
    """
    if not local_files:
        return 0.0
    ls = {(p.rsplit("/", 1)[-1], s) for p, s in local_files if p}
    cs = {(p.rsplit("/", 1)[-1], s) for p, s in cand_files if p}
    if not ls:
        return 0.0
    return len(ls & cs) / len(ls)


class SkipSite(Exception):
    """站点处于流控冷却期, 跳过该站点。"""


class RateLimiter:
    """站点级流控:最小间隔 + 每分钟配额 + 失败冷却 + 全局节流。

    防站点管控(实证各 PT 站对频繁搜索/下载有风控):
    - min_interval: 同一站点任意两次请求的最小间隔(秒)
    - per_minute: 同一站点每分钟请求上限
    - global_interval: 全局(所有站点合计)最小间隔(秒)
    - cooldown_seconds: 站点请求失败(429/超时/5xx)后冷却时长, 冷却期内跳过该站
    """

    def __init__(self, min_interval: float = 2.0, per_minute: int = 8,
                 global_interval: float = 1.0, cooldown_seconds: float = 120.0):
        self.min_interval = min_interval
        self.per_minute = per_minute
        self.global_interval = global_interval
        self.cooldown_seconds = cooldown_seconds
        self._last: Dict[str, float] = {}        # key -> 上次请求时间
        self._window: Dict[str, List[float]] = {}  # key -> 滑动窗口时间戳
        self._cool_until: Dict[str, float] = {}   # indexer -> 冷却截止时间
        self._last_global = 0.0

    @staticmethod
    def _key(kind: str, indexer: str) -> str:
        return f"{kind}:{indexer}"

    def in_cooldown(self, indexer: str) -> bool:
        return self._cool_until.get(indexer, 0.0) > time.time()

    def cooldown_site(self, indexer: str) -> None:
        """站点请求失败 → 进入冷却期。"""
        self._cool_until[indexer] = time.time() + self.cooldown_seconds

    def acquire(self, kind: str, indexer: str, now: float | None = None) -> float:
        """等待直到允许请求, 返回实际等待秒数。站点冷却中抛 SkipSite。"""
        if self.in_cooldown(indexer):
            raise SkipSite(indexer)
        now = now if now is not None else time.time()
        key = self._key(kind, indexer)
        wait = self.min_interval - (now - self._last.get(key, 0.0))
        # 滑动窗口配额:满则等到最早请求过期(60s 窗口)
        window = self._window.setdefault(key, [])
        cutoff = now - 60.0
        window[:] = [ts for ts in window if ts > cutoff]
        if len(window) >= self.per_minute:
            wait = max(wait, window[0] + 60.0 - now)
        # 全局节流
        wait = max(wait, self.global_interval - (now - self._last_global))
        if wait > 0:
            time.sleep(wait)
            now = time.time()
        self._last[key] = now
        self._last_global = now
        self._window.setdefault(key, []).append(now)
        return wait


class Matcher(ABC):
    """匹配器接口(未来可扩展 IYUU 等实现)。"""

    @abstractmethod
    def match(self, torrent: TorrentInfo, local_files: List[Tuple[str, int]],
              candidates_limit: int) -> List[Candidate]:
        """按标题+大小+文件列表找同源候选。"""

    @abstractmethod
    def download(self, url: str) -> Optional[bytes]:
        """下载候选种子字节(执行阶段用)。"""

    def site_from_tracker(self, tracker_url: str) -> Optional[str]:
        """由 tracker 地址识别站点(Jackett 索引器 id); 识别不到返回 None。"""
        if not tracker_url:
            return None
        domain = urlparse(tracker_url).netloc.lower()
        for keyword, indexer in DEFAULT_TRACKER_MAP.items():
            if keyword.lower() in domain:
                return indexer
        return None


class JackettMatcher(Matcher):
    def __init__(self, conf: JackettConf):
        self.conf = conf
        # 下载超时:种子经 Jackett 从站点中转下载, 慢站 30s 内可完成(实证 hdarea 17s)
        self._client = httpx.Client(
            base_url=conf.url.rstrip("/"), timeout=30.0, follow_redirects=True
        )
        # 站点级流控(防管控)
        self._limiter = RateLimiter(
            min_interval=conf.per_indexer_delay,
            per_minute=conf.per_minute,
            global_interval=conf.global_interval,
            cooldown_seconds=conf.cooldown_seconds,
        )
        # tracker 域名 → 索引器 id 映射(内置默认 + 配置覆盖)
        self._tracker_map = dict(DEFAULT_TRACKER_MAP)
        self._tracker_map.update(conf.tracker_map or {})

    def site_from_tracker(self, tracker_url: str) -> Optional[str]:
        """由 tracker 地址识别站点(内置默认 + 配置覆盖)。"""
        if not tracker_url:
            return None
        domain = urlparse(tracker_url).netloc.lower()
        for keyword, indexer in self._tracker_map.items():
            if keyword.lower() in domain:
                return indexer
        return None

    # ---------------- 流控 ----------------

    def _acquire(self, kind: str, indexer: str) -> bool:
        """等待流控放行;站点冷却中返回 False(调用方跳过该站)。"""
        try:
            self._limiter.acquire(kind, indexer)
            return True
        except SkipSite:
            logger.warning(f"[reseed] 站点流控冷却中, 跳过 [{indexer}]")
            return False

    def _site_from_url(self, url: str) -> str:
        """从 Jackett 下载链接解析站点 id(形如 /dl/{indexer}/...)。"""
        path = urlparse(url).path
        parts = path.split("/")
        for i, p in enumerate(parts):
            if p == "dl" and i + 1 < len(parts):
                return parts[i + 1]
        return "__unknown__"

    # ---------------- 匹配 ----------------

    def match(self, torrent: TorrentInfo, local_files: List[Tuple[str, int]],
              candidates_limit: int, skip_indexers: Optional[set] = None) -> List[Candidate]:
        """按标题+大小+文件列表找同源候选。

        skip_indexers: 已覆盖站点(组内副本 tracker 识别), 跳过其搜索。
        """
        results: List[Candidate] = []
        queries = build_search_queries(torrent.name)
        skip_indexers = skip_indexers or set()
        for indexer in self.conf.indexers:
            if len(results) >= candidates_limit:
                break
            if indexer in skip_indexers:
                logger.info(f"[reseed] [{indexer}] 已覆盖(组内存在该站副本), 跳过搜索")
                continue
            if len(results) >= candidates_limit:
                break
            if not self._acquire("search", indexer):
                continue  # 站点冷却中, 跳过
            downloads = 0  # 本索引器文件比对下载预算计数(必须每索引器重置)
            # 合并所有搜索词的结果, 按 (title, size) 去重(download_url 可能因代理参数
            # 不同而不同, 同一候选会以不同链接重复出现); 空结果重试 1 次容忍站端临时空窗
            items: List[Candidate] = []
            seen: set = set()
            for query in queries:
                found: List[Candidate] = []
                for attempt in range(2):
                    try:
                        found = self._search(indexer, query)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[reseed] 索引器搜索失败 {indexer}: {e}")
                        found = []
                    if found:
                        break
                    time.sleep(RETRY_SLEEP)
                logger.info(f"[reseed] [{indexer}] 搜索 '{query}' → {len(found)} 条")
                for it in found:
                    key = (it.title, it.size)
                    if key not in seen:
                        seen.add(key)
                        items.append(it)
            logger.info(f"[reseed] [{indexer}] 去重后 {len(items)} 条候选, 大小容差内 "
                       f"{sum(1 for it in items if self._size_ok(it.size, torrent.size))} 条")
            for item in items:
                if len(results) >= candidates_limit:
                    break
                # 大小容差过滤
                if not self._size_ok(item.size, torrent.size):
                    continue
                # 同 infohash 快速命中(原样转载的情况,零下载)
                if item.info_hash and item.info_hash.lower() == torrent.hash.lower():
                    results.append(item)
                    continue
                # 文件级匹配:下载候选种子,解析文件列表与本地比对(受预算限制)
                if downloads >= MAX_DOWNLOAD_PER_INDEXER:
                    continue
                downloads += 1
                try:
                    data = self._download(item.download_url)
                    if not data:
                        continue
                    cand_files = file_list(data)
                    if not cand_files:
                        continue
                    if match_ratio(local_files, cand_files) < MATCH_RATIO_THRESHOLD:
                        continue
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[reseed] 候选种子文件比对失败 {item.download_url}: {e}")
                    continue
                results.append(item)
                logger.info(f"[reseed] [{indexer}] 命中同源候选: {item.title[:60]} ({item.size/1e9:.2f}GB)")
        return results

    # ---------------- 私有 ----------------

    def _search(self, indexer: str, query: str) -> List[Candidate]:
        if not self._acquire("search", indexer):
            raise SkipSite(indexer)
        params = {"apikey": self.conf.api_key, "q": query}
        resp = self._client.get(
            f"/api/v2.0/indexers/{indexer}/results/torznab", params=params
        )
        if resp.status_code in (429, 403):
            self._limiter.cooldown_site(indexer)   # 风控/限流 → 冷却该站
            raise RuntimeError(f"站点风控 [{indexer}]: HTTP {resp.status_code}")
        resp.raise_for_status()
        return self._parse_torznab(resp.content, indexer)

    @staticmethod
    def _parse_torznab(xml_bytes: bytes, indexer: str) -> List[Candidate]:
        root = ET.fromstring(xml_bytes)
        items: List[Candidate] = []
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            size = 0
            try:
                size = int(item.findtext("size") or 0)
            except ValueError:
                size = 0
            infohash = ""
            seeders = 0
            for attr in item.findall(f"{_TORZNAB_NS}attr"):
                name = attr.get("name") or ""
                value = attr.get("value") or ""
                if name == "infohash":
                    infohash = value
                elif name == "seeders":
                    try:
                        seeders = int(value)
                    except ValueError:
                        seeders = 0
            items.append(Candidate(
                indexer=indexer, torrent_id=link, title=title, size=size,
                download_url=link, info_hash=infohash, seeders=seeders,
            ))
        return items

    def _size_ok(self, candidate_size: int, torrent_size: int) -> bool:
        if torrent_size <= 0:
            return True  # 源大小未知,不按大小过滤
        diff = abs(candidate_size - torrent_size) / torrent_size
        return diff <= self.conf.size_tolerance

    def _download(self, url: str) -> Optional[bytes]:
        """下载候选种子字节(内部)。"""
        return self.download(url)

    def download(self, url: str) -> Optional[bytes]:
        """下载候选种子字节(失败重试 1 次 + 站点冷却)。链接为 Jackett 代理时补 apikey。"""
        site = self._site_from_url(url)
        if not self._acquire("download", site):
            return None
        if urlparse(url).netloc == urlparse(self.conf.url).netloc:
            sep = "&" if "?" in url else "?"
            if "apikey=" not in url:
                url = f"{url}{sep}{urlencode({'apikey': self.conf.api_key})}"
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                resp = self._client.get(url)
                if resp.status_code in (429, 403):
                    self._limiter.cooldown_site(site)   # 风控/限流 → 冷却该站
                    raise RuntimeError(f"站点风控 [{site}]: HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.content
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < 1:
                    time.sleep(1.0)
        # 持续失败 → 冷却该站, 避免继续打请求触发管控
        self._limiter.cooldown_site(site)
        logger.warning(f"[reseed] 候选种子下载失败(重试1次+冷却): {last_err}")
        return None

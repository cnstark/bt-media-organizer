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


class Matcher(ABC):
    """匹配器接口(未来可扩展 IYUU 等实现)。"""

    @abstractmethod
    def match(self, torrent: TorrentInfo, local_files: List[Tuple[str, int]],
              candidates_limit: int) -> List[Candidate]:
        """按标题+大小+文件列表找同源候选。"""

    @abstractmethod
    def download(self, url: str) -> Optional[bytes]:
        """下载候选种子字节(执行阶段用)。"""


class JackettMatcher(Matcher):
    def __init__(self, conf: JackettConf):
        self.conf = conf
        # 下载超时:种子经 Jackett 从站点中转下载, 慢站 30s 内可完成(实证 hdarea 17s)
        self._client = httpx.Client(
            base_url=conf.url.rstrip("/"), timeout=30.0, follow_redirects=True
        )
        self._last_request: dict = {}   # indexer -> 上次请求时间

    # ---------------- 限速 ----------------

    def _throttle(self, indexer: str) -> None:
        last = self._last_request.get(indexer, 0.0)
        gap = time.time() - last
        wait = self.conf.per_indexer_delay - gap
        if wait > 0:
            time.sleep(wait)
        self._last_request[indexer] = time.time()

    # ---------------- 匹配 ----------------

    def match(self, torrent: TorrentInfo, local_files: List[Tuple[str, int]],
              candidates_limit: int) -> List[Candidate]:
        results: List[Candidate] = []
        queries = build_search_queries(torrent.name)
        for indexer in self.conf.indexers:
            if len(results) >= candidates_limit:
                break
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
                for it in found:
                    key = (it.title, it.size)
                    if key not in seen:
                        seen.add(key)
                        items.append(it)
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
        return results

    # ---------------- 私有 ----------------

    def _search(self, indexer: str, query: str) -> List[Candidate]:
        self._throttle(indexer)
        params = {"apikey": self.conf.api_key, "q": query}
        resp = self._client.get(
            f"/api/v2.0/indexers/{indexer}/results/torznab", params=params
        )
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
        """下载候选种子字节(失败重试 2 次)。链接为 Jackett 代理时补 apikey。"""
        self._throttle("__download__")
        if urlparse(url).netloc == urlparse(self.conf.url).netloc:
            sep = "&" if "?" in url else "?"
            if "apikey=" not in url:
                url = f"{url}{sep}{urlencode({'apikey': self.conf.api_key})}"
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                resp = self._client.get(url)
                resp.raise_for_status()
                return resp.content
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < 1:
                    time.sleep(1.0)
        logger.warning(f"[reseed] 候选种子下载失败(重试2次): {last_err}")
        return None

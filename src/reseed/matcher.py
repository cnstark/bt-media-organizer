"""辅种匹配器:Matcher 接口 + Jackett 实现(Torznab 搜索 → 大小容差 → infohash 比对)。"""
from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlencode, urlparse

import httpx

from ..config import JackettConf
from ..downloaders.base import TorrentInfo
from ..downloaders.bencode import info_hash

logger = logging.getLogger("bt-media-organizer.reseed.matcher")

_TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"


@dataclass
class Candidate:
    """匹配到的可辅种候选。"""

    indexer: str          # Jackett 索引器 id
    torrent_id: str       # 结果标识(无稳定 id 时用 link)
    title: str
    size: int
    download_url: str
    info_hash: str = ""   # Torznab 直接返回时非空
    seeders: int = 0


class Matcher(ABC):
    """匹配器接口(未来可扩展 IYUU 等实现)。"""

    @abstractmethod
    def match(self, torrent: TorrentInfo, candidates_limit: int) -> List[Candidate]:
        """按标题+大小找同 infohash 的候选。"""

    @abstractmethod
    def download(self, url: str) -> Optional[bytes]:
        """下载候选种子字节(执行阶段用)。"""


class JackettMatcher(Matcher):
    def __init__(self, conf: JackettConf):
        self.conf = conf
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

    def match(self, torrent: TorrentInfo, candidates_limit: int) -> List[Candidate]:
        results: List[Candidate] = []
        for indexer in self.conf.indexers:
            if len(results) >= candidates_limit:
                break
            try:
                items = self._search(indexer, torrent.name)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[reseed] 索引器搜索失败 {indexer}: {e}")
                continue
            for item in items:
                if len(results) >= candidates_limit:
                    break
                # 大小容差过滤
                if not self._size_ok(item.size, torrent.size):
                    continue
                # infohash 比对
                if item.info_hash:
                    if item.info_hash.lower() != torrent.hash.lower():
                        continue
                else:
                    # 无 infohash 属性 → 下载候选种子本地比对
                    try:
                        data = self._download(item.download_url)
                        if data is None or info_hash(data) != torrent.hash:
                            continue
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[reseed] 候选种子校验失败 {item.download_url}: {e}")
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
        """下载候选种子字节。链接为 Jackett 代理时补 apikey。"""
        self._throttle("__download__")
        if urlparse(url).netloc == urlparse(self.conf.url).netloc:
            sep = "&" if "?" in url else "?"
            if "apikey=" not in url:
                url = f"{url}{sep}{urlencode({'apikey': self.conf.api_key})}"
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp.content

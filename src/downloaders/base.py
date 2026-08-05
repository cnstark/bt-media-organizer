"""下载器适配器协议。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class TorrentInfo:
    """统一下载器任务信息(轮询对账用)。"""

    hash: str
    name: str
    save_path: Path
    content_path: Path
    category: str = ""
    tags: List[str] = field(default_factory=list)
    size: int = 0
    state: str = ""


@dataclass
class WebhookEvent:
    """统一 webhook 完成事件。"""

    event: str = "torrent_finished"
    hash: str = ""
    name: str = ""
    save_path: Path = Path("")
    content_path: Path = Path("")
    downloader: str = ""


class DownloaderAdapter(ABC):
    """下载器适配器基类。新增下载器时实现本协议即可。"""

    name: str = ""

    @abstractmethod
    def list_finished(self) -> List[TorrentInfo]:
        """轮询:返回「已下载完成」的任务列表(未过滤标签,由调用方过滤)。"""

    @abstractmethod
    def add_tag(self, hash: str) -> bool:
        """打整理完成标签(对账依据)。"""

    @abstractmethod
    def delete_torrent(self, hash: str, delete_files: bool = True) -> bool:
        """删除下载器任务(预留,默认不使用)。"""

    @abstractmethod
    def parse_webhook(self, payload: dict) -> Optional[WebhookEvent]:
        """把下载器 webhook 报文归一化为 WebhookEvent;非完成事件返回 None。"""

    @staticmethod
    def has_tag(torrent: TorrentInfo, tag: str) -> bool:
        return tag in torrent.tags

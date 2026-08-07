"""下载器适配器协议(v2:扩展转移/辅种所需接口)。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Union


@dataclass
class TorrentInfo:
    """统一下载器任务信息。"""

    hash: str                       # v1 infohash(hex 小写)
    name: str
    save_path: Path                 # 保存目录(下载器视角)
    content_path: Path              # 内容根路径(qb: content_path;tr: downloadDir/name 近似)
    category: str = ""
    tags: List[str] = field(default_factory=list)
    size: int = 0
    state: str = ""                 # 下载器原始状态文本
    tracker: str = ""               # 主 tracker announce(fastresume 补丁用)
    infohash_v1: str = ""           # qB v4.4+ 混合种子 v1 hash(种子文件兜底用)
    torrent_file: str = ""          # tr: RPC 返回的种子文件路径;qb: BT_backup 候选
    done: bool = False              # 是否已完成
    seeding: bool = False           # 是否在做种(转移/辅种候选集合依据)


class DownloaderAdapter(ABC):
    """下载器适配器基类。新增下载器时实现本协议即可。"""

    name: str = ""

    # ---------------- 整理模块(现状) ----------------

    @abstractmethod
    def list_finished(self) -> List[TorrentInfo]:
        """轮询:返回「已下载完成」的任务列表(未过滤标签,由调用方过滤)。"""

    @abstractmethod
    def add_tag(self, hash: str) -> bool:
        """打整理完成标签(对账依据)。"""

    @abstractmethod
    def delete_torrent(self, hash: str, delete_files: bool = True) -> bool:
        """删除下载器任务。"""

    @staticmethod
    def has_tag(torrent: TorrentInfo, tag: str) -> bool:
        return tag in torrent.tags

    # ---------------- v2:转移/辅种 ----------------

    @abstractmethod
    def list_torrents(self, state: str = "all") -> List[TorrentInfo]:
        """全部/做种/完成列表。state: all | seeding | completed。"""

    @abstractmethod
    def get_torrent_file(self, hash: str) -> Optional[bytes]:
        """读取种子文件字节(转移用)。取不到返回 None。"""

    @abstractmethod
    def get_torrent_files(self, hash: str) -> List[tuple]:
        """种子文件列表 [(相对路径, 大小)](辅种文件级匹配用)。"""

    @abstractmethod
    def add_torrent(self, data: Union[bytes, str], save_path: str, *,
                    paused: bool, category: str = "", tags: Optional[List[str]] = None,
                    skip_checking: bool = False) -> Tuple[bool, str]:
        """添加种子。data: bytes=元数据上传;str 且以 http 开头=URL。
        返回 (ok, message)。"""

    @abstractmethod
    def recheck(self, hash: str) -> bool:
        """触发重新校验。"""

    @abstractmethod
    def app_version(self) -> str:
        """下载器版本字符串(如 qB >=4.4 判定)。"""

    @abstractmethod
    def has_torrent(self, hash: str) -> bool:
        """目标下载器是否已存在该 hash(幂等判断)。"""

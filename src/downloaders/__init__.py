"""下载器子包。"""
from .base import DownloaderAdapter, TorrentInfo
from .qbittorrent import QBittorrentAdapter

__all__ = ["DownloaderAdapter", "TorrentInfo", "QBittorrentAdapter"]

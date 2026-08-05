"""下载器子包。"""
from .base import DownloaderAdapter, TorrentInfo, WebhookEvent
from .qbittorrent import QBittorrentAdapter

__all__ = ["DownloaderAdapter", "TorrentInfo", "WebhookEvent", "QBittorrentAdapter"]

"""下载器子包。"""
from .base import DownloaderAdapter, TorrentInfo
from .qbittorrent import QBittorrentAdapter
from .transmission import TransmissionAdapter

__all__ = ["DownloaderAdapter", "TorrentInfo", "QBittorrentAdapter", "TransmissionAdapter", "create_adapter"]


def create_adapter(conf) -> DownloaderAdapter:
    """按配置类型创建下载器适配器。"""
    if conf.type == "qbittorrent":
        return QBittorrentAdapter(conf)
    if conf.type == "transmission":
        return TransmissionAdapter(conf)
    raise ValueError(f"不支持的下载器类型: {conf.type}")

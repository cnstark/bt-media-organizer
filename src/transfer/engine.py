"""转移引擎:轮询主循环 + 单种子转移(无记录表,幂等靠目标下载器状态)。"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from ..config import TransferConf
from ..downloaders.base import DownloaderAdapter, TorrentInfo
from ..downloaders.bencode import extract_announce, patch_announce
from .pathrule import convert_path, match_path

logger = logging.getLogger("bt-media-organizer.transfer")

# 标记规则 → 注入参数(与 IYUU 命名一致,便于识别)
MARKER_TAG = "已转移"


class TransferEngine:
    def __init__(self, conf: TransferConf, from_adapter: DownloaderAdapter,
                 to_adapter: DownloaderAdapter):
        self.conf = conf
        self.from_adapter = from_adapter
        self.to_adapter = to_adapter
        self._lock = threading.Lock()
        self.last_run: Optional[float] = None   # 最近一次轮询完成时间
        self.last_stats: Dict = {}

    # ---------------- 对外 ----------------

    def run_once(self) -> dict:
        """轮询主循环:来源做种列表 → 逐个转移。返回统计。"""
        with self._lock:
            stats = {"total": 0, "transferred": 0, "skipped": 0, "failed": 0}
            try:
                torrents = self.from_adapter.list_torrents(state="seeding")
            except Exception as e:  # noqa: BLE001
                logger.error(f"[transfer] 获取来源做种列表失败: {e}")
                return stats
            stats["total"] = len(torrents)
            for t in torrents:
                ok, msg = self.transfer_one(t)
                if ok:
                    stats["transferred"] += 1
                elif msg == "skipped":
                    stats["skipped"] += 1
                else:
                    stats["failed"] += 1
                    logger.warning(f"[transfer] 转移失败 {t.hash} {t.name}: {msg}")
            self.last_run = time.time()
            self.last_stats = stats
            logger.info(f"[transfer] 轮询完成: {stats}")
            return stats

    def transfer_one(self, t: TorrentInfo) -> tuple:
        """单种子转移。返回 (ok, message);message='skipped' 表示幂等/过滤跳过。"""
        # 1. 幂等:目标下载器已存在同 hash → 跳过
        try:
            if self.to_adapter.has_torrent(t.hash):
                return False, "skipped"
        except Exception as e:  # noqa: BLE001
            logger.error(f"[transfer] 查询目标下载器失败 {t.hash}: {e}")
            return False, f"1.查询目标下载器失败 {e}"
        # 2. 路径过滤/选择
        if not match_path(str(t.save_path), self.conf.path.filter_paths,
                          self.conf.path.selector_paths):
            return False, "skipped"
        # 3. 路径转换
        target_path = convert_path(str(t.save_path), self.conf.path.convert_type,
                                   self.conf.path.rules)
        if not target_path:
            return False, f"3.路径转换失败: {t.save_path} (convert_type={self.conf.path.convert_type})"
        # 4. 读取来源种子文件(含 announce 修补)
        data = self._read_torrent(t)
        if data is None:
            return False, f"4.读取种子文件失败: {t.hash} (请检查下载器 torrent_path 配置)"
        # 5. 注入目标下载器
        ok, msg = self._inject(data, target_path)
        if not ok:
            return False, f"5.注入失败: {msg}"
        # 6. 成功 → 可选删除来源种子(只删种子不删数据)
        if self.conf.delete_source:
            try:
                self.from_adapter.delete_torrent(t.hash, delete_files=False)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[transfer] 删除来源种子失败 {t.hash}(已转移成功): {e}")
        logger.info(f"[transfer] 转移成功 {t.hash} {t.name} → {target_path}")
        return True, "ok"

    # ---------------- 私有 ----------------

    def _read_torrent(self, t: TorrentInfo) -> Optional[bytes]:
        """读取种子文件;announce 为空时补 tracker(qB API / fastresume 逻辑在适配器层)。"""
        try:
            data = self.from_adapter.get_torrent_file(t.hash)
            # TR 兜底:RPC 的 torrentFile 不可达时,用配置种子目录 + 文件名
            if data is None and t.torrent_file and getattr(self.from_adapter.conf, "torrent_path", ""):
                fallback = Path(self.from_adapter.conf.torrent_path) / Path(t.torrent_file).name
                try:
                    data = fallback.read_bytes()
                except OSError:
                    pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"[transfer] 读取种子文件异常 {t.hash}: {e}")
            return None
        if data is None:
            return None
        # announce 修补
        try:
            if not extract_announce(data):
                announce = t.tracker or self._fetch_tracker(t)
                if announce:
                    data = patch_announce(data, announce)
                else:
                    logger.warning(f"[transfer] 种子无 announce 且取不到 tracker,跳过修补: {t.hash}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[transfer] announce 修补失败 {t.hash}: {e}")
        return data

    def _fetch_tracker(self, t: TorrentInfo) -> str:
        getter = getattr(self.from_adapter, "get_tracker", None)
        if callable(getter):
            try:
                return getter(t.hash)
            except Exception:  # noqa: BLE001
                return ""
        return ""

    def _inject(self, data: bytes, target_path: str) -> tuple:
        category, tags = "", None
        if self.conf.marker == "category":
            category = MARKER_TAG
        elif self.conf.marker == "tag":
            tags = [MARKER_TAG]
        try:
            return self.to_adapter.add_torrent(
                data, target_path,
                paused=not self.conf.auto_start,
                category=category, tags=tags,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[transfer] 注入异常: {e}")
            return False, str(e)

"""辅种引擎:匹配 + 执行两阶段,幂等靠记录唯一键,失败自动重试。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

from ..config import ReseedConf
from ..downloaders.base import DownloaderAdapter
from ..transfer.pathrule import match_path
from .matcher import Candidate, Matcher
from .store import ReseedRecord, ReseedStore

logger = logging.getLogger("bt-media-organizer.reseed")

# 标记规则 → 注入参数(辅种专用标记,便于识别与清理)
RESEED_MARKER = "辅种"

# 单轮匹配上限(控制搜索成本;搜索式辅种每种子需多站多次搜索+候选下载, 单轮预算不宜过大)
MAX_MATCH_PER_ROUND = 10
# 单轮执行上限
MAX_EXECUTE_PER_ROUND = 20


class ReseedEngine:
    def __init__(self, conf: ReseedConf, adapters: Dict[str, DownloaderAdapter],
                 store: ReseedStore, matcher: Matcher):
        self.conf = conf
        self.adapters = adapters
        self.target = adapters[conf.target_client]
        self.store = store
        self.matcher = matcher
        self._lock = threading.Lock()
        self._round = 0
        self.last_run: Optional[float] = None
        self.last_stats: Dict = {}

    # ---------------- 对外 ----------------

    def run_once(self) -> dict:
        with self._lock:
            stats = {"matched": 0, "injected": 0, "failed": 0,
                     "skipped": 0, "no_match": 0, "pending": 0}
            self._round += 1
            self._match_phase(stats)
            self._execute_phase(stats)
            self.last_run = time.time()
            self.last_stats = stats
            logger.info(f"[reseed] 轮询完成: {stats}")
            return stats

    def redo(self, record_id: int) -> tuple:
        """失败/跳过记录置回 pending 并立即执行。返回 (ok, message)。"""
        with self._lock:
            row = self.store.get(record_id)
            if not row:
                return False, "记录不存在"
            if row.status not in ("failed", "skipped"):
                return False, f"当前状态 {row.status} 不允许重试"
            ok, msg = self._execute(row)
            self.store.update_status(row.id, "success" if ok else "failed", msg)
            return ok, msg

    # ---------------- 匹配阶段 ----------------

    def _covered_indexers(self, g: dict) -> set:
        """组内副本 tracker 识别出的已覆盖站点(仅白名单内)。"""
        covered = set()
        for tracker in g.get("trackers", set()):
            indexer = self.matcher.site_from_tracker(tracker)
            if indexer:
                covered.add(indexer)
        whitelist = set(self.conf.jackett.indexers)
        return covered & whitelist

    def _match_phase(self, stats: dict) -> None:
        budget = MAX_MATCH_PER_ROUND
        logger.info(f"[reseed] 匹配阶段开始: 单轮预算 {budget} 个发布组")
        for name, adapter in self.adapters.items():
            if budget <= 0:
                break
            # 注意:目标下载器自身的做种也参与匹配(文件级匹配产出的是不同 infohash
            # 的同源种子, 可在同一客户端共存做种——TR 内 783 条跨站共存记录为证)
            try:
                torrents = adapter.list_torrents(state="seeding")
            except Exception as e:  # noqa: BLE001
                logger.error(f"[reseed] 获取做种列表失败 [{name}]: {e}")
                continue
            n = len(torrents)
            logger.info(f"[reseed] [{name}] 做种列表 {n} 个")
            if n == 0:
                continue
            # 组内去重: 同名同大小 = 同一发布的跨站副本, 整组只匹配一次,
            # 避免每个副本都重复搜索(TR 内 783 个种子仅 130 个发布组)
            groups: dict = {}
            for t in torrents:
                key = (t.name, t.size)
                g = groups.setdefault(key, {"rep": t, "hashes": set(), "trackers": set()})
                g["hashes"].add(t.hash)
                if t.tracker:
                    g["trackers"].add(t.tracker)
            group_list = list(groups.values())
            total = len(group_list)
            logger.info(f"[reseed] [{name}] 组内去重后 {total} 个发布组")
            # 轮询错峰:每轮从不同起点开始,配合预算覆盖全部发布组
            start = (self._round * 7) % total
            order = group_list[start:] + group_list[:start]
            for idx, g in enumerate(order, 1):
                if budget <= 0:
                    break
                t = g["rep"]
                covered = self._covered_indexers(g)
                logger.info(f"[reseed] 匹配 [{idx}/{total}] 发布组: {t.name[:55]} "
                            f"(跨站副本{len(g['hashes'])}个, 源={name}, 预算剩{budget})")
                if covered:
                    logger.info(f"[reseed] 发布组已覆盖站点(跳过搜索): {sorted(covered)}")
                if not match_path(str(t.save_path), self.conf.exclude_paths, []):
                    logger.info(f"[reseed] 发布组被排除目录过滤: {t.name[:50]}")
                    continue  # 排除目录
                # 组级已处理: 组内任意副本已有记录 → 整组跳过, 不再重复扫描
                rows = self.store.list_by_sources(self.target.name, list(g["hashes"]))
                if any(r.status in ("pending", "success", "skipped") for r in rows):
                    logger.info(f"[reseed] 发布组已处理过, 跳过 (组内副本{len(g['hashes'])}个)")
                    stats["skipped"] += 1
                    continue
                # 目标下载器已有组内副本(仅非目标源检查:如 qB 种子已被转移进 TR 则无需再辅;
                # 目标自身种子必然在目标中, 跳过此检查直接参与匹配——匹配产出是不同
                # infohash 的同源种子, 与自身共存做种)
                if adapter is not self.target:
                    try:
                        if any(self.target.has_torrent(h) for h in g["hashes"]):
                            for h in g["hashes"]:
                                self.store.add(client_id=self.target.name, source_hash=h,
                                               info_hash=h, directory=str(t.save_path),
                                               status="skipped", message="目标下载器已有同 hash")
                            logger.info(f"[reseed] 发布组在目标下载器已有副本, 跳过: {t.name[:50]}")
                            stats["skipped"] += 1
                            continue
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"[reseed] 查询目标下载器失败 {t.hash}: {e}")
                        continue
                # 匹配
                try:
                    local_files = adapter.get_torrent_files(t.hash)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[reseed] 获取本地文件列表失败 {t.hash}: {e}")
                    continue
                if not local_files:
                    logger.warning(f"[reseed] 发布组本地文件列表为空, 无法匹配: {t.name[:50]} (hash={t.hash[:12]})")
                    continue
                try:
                    cands = self.matcher.match(t, local_files, self.conf.jackett.max_candidates,
                                               skip_indexers=self._covered_indexers(g))
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[reseed] 发布组匹配异常: {t.name[:50]} 原因: {e}")
                    continue
                if not cands:
                    stats["no_match"] += 1
                    logger.info(f"[reseed] 发布组无匹配候选: {t.name[:50]} (各站搜索后无同源, 可能仅本站独有)")
                    continue  # 不落库,下轮错峰重试
                matched_cnt = 0
                for c in cands:
                    # 已处理(含失败重试:failed 重置为 pending)
                    existing = self.store.get_by_hash(self.target.name, c.info_hash)
                    if existing:
                        if existing.status in ("pending", "success", "skipped"):
                            continue
                        # failed → 重置为 pending,由执行阶段重试
                        self.store.update_status(existing.id, "pending", None)
                        stats["matched"] += 1
                        continue
                    try:
                        if self.target.has_torrent(c.info_hash):
                            self.store.add(client_id=self.target.name, source_hash=t.hash,
                                           info_hash=c.info_hash, directory=str(t.save_path),
                                           site=c.indexer, status="skipped",
                                           message="目标下载器已有同 hash")
                            stats["skipped"] += 1
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    rid = self.store.add(
                        client_id=self.target.name, source_hash=t.hash,
                        info_hash=c.info_hash, directory=str(t.save_path),
                        site=c.indexer, torrent_id=c.torrent_id,
                        marker=self.conf.marker,
                        payload={"download_url": c.download_url, "title": c.title},
                    )
                    if rid:
                        matched_cnt += 1
                        stats["matched"] += 1
                        # 立即执行该候选(下载种子→注入目标下载器), 不等整轮匹配完成
                        row = self.store.get(rid)
                        ok, msg = self._execute(row)
                        self.store.update_status(rid, "success" if ok else "failed", msg)
                        if ok:
                            stats["injected"] += 1
                            title = row.payload_dict().get("title") or c.info_hash
                            logger.info(f"[reseed] 注入成功: [{row.site}] {str(title)[:55]} → {row.directory}")
                        else:
                            stats["failed"] += 1
                            logger.warning(f"[reseed] 注入失败 {c.info_hash} [{row.site}]: {msg}")
                if matched_cnt:
                    logger.info(f"[reseed] 发布组匹配成功: {t.name[:50]} 入队 {matched_cnt} 个候选")
                else:
                    logger.info(f"[reseed] 发布组候选全部已存在/已处理: {t.name[:50]} "
                                f"(搜索到 {len(cands)} 候选但均无需注入)")
                budget -= 1

    # ---------------- 执行阶段 ----------------

    def _execute_phase(self, stats: dict) -> None:
        rows = self.store.list(status="pending", limit=MAX_EXECUTE_PER_ROUND)
        if rows:
            logger.info(f"[reseed] 执行阶段: {len(rows)} 个待注入")
        for row in rows:
            ok, msg = self._execute(row)
            self.store.update_status(row.id, "success" if ok else "failed", msg)
            if ok:
                stats["injected"] += 1
                title = row.payload_dict().get("title") or row.info_hash
                logger.info(f"[reseed] 注入成功: [{row.site}] {str(title)[:55]} → {row.directory}")
            else:
                stats["failed"] += 1
                logger.warning(f"[reseed] 注入失败 {row.info_hash} [{row.site}]: {msg}")
        stats["pending"] = len(self.store.list(status="pending", limit=1))

    def _execute(self, row: ReseedRecord) -> tuple:
        """下载候选种子并注入目标下载器。返回 (ok, message)。"""
        url = row.payload_dict().get("download_url") or ""
        if not url:
            return False, "1.记录缺少 download_url"
        try:
            data = self.matcher.download(url)
        except Exception as e:  # noqa: BLE001
            return False, f"2.下载种子失败: {e}"
        if not data:
            return False, "2.下载种子失败: 空响应"
        category, tags = "", None
        if self.conf.marker == "category":
            category = RESEED_MARKER
        elif self.conf.marker == "tag":
            tags = [RESEED_MARKER]
        try:
            ok, msg = self.target.add_torrent(
                data, row.directory,
                paused=not self.conf.auto_start,   # 默认暂停,校验后由用户开始
                category=category, tags=tags,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"3.注入异常: {e}"
        if not ok:
            return False, f"3.注入失败: {msg}"
        if self.conf.check_on_add:
            try:
                self.target.recheck(row.info_hash)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[reseed] recheck 失败 {row.info_hash}: {e}")
        return True, "ok"

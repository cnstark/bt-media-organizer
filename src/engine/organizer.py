"""整理引擎:目录匹配 → 规划 → 识别 → 幂等 → 执行 → 历史 → 打标签。

参照 MoviePilot `TransferChain.do_transfer` 与 `__default_callback` 的简化实现。
失败自愈:失败文件不写 success → 该 torrent 不打标签 → 下轮轮询自动重试。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ..config import Config, TransferDirConf
from ..downloaders import DownloaderAdapter, QBittorrentAdapter, TorrentInfo, WebhookEvent
from ..history import HistoryStore
from ..parse.filename import parse_filename, subtitle_lang_tag
from ..recognize.tmdb import MediaInfo, TmdbRecognizer
from ..storage import local
from . import executor
from .namer import render_path
from .planner import PlanItem, is_bluray_dir, plan

logger = logging.getLogger("lite-organizer.engine")


@dataclass
class OrganizeResult:
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    all_success: bool = True
    preview: bool = False
    message: str = ""
    items: List[dict] = field(default_factory=list)  # {source, target, success, message, kind}


class TransferEngine:
    """整理引擎(单例使用)。"""

    def __init__(self, conf: Config, store: HistoryStore):
        self.conf = conf
        self.store = store
        self.recognizer = TmdbRecognizer(conf.recognize.tmdb, store)
        self._processing: set = set()           # 处理中的 torrent hash
        self._processing_lock = threading.Lock()
        self._recent: List[dict] = []           # 最近整理结果(API 查询用)
        self._main_dest_cache: dict = {}        # 附加文件目标路径缓存(每次 organize 重置)
        self.downloaders: Dict[str, DownloaderAdapter] = {}
        for dl in conf.downloaders:
            if dl.type == "qbittorrent":
                self.downloaders[dl.name] = QBittorrentAdapter(dl)

    # ------------------------------------------------------------ 工具

    def match_dir(self, source: Path) -> Optional[TransferDirConf]:
        """源路径前缀匹配下载目录配置(按配置顺序,命中即止)。"""
        for d in self.conf.directories:
            if not d.monitor or not d.download_path:
                continue
            try:
                if source.is_relative_to(Path(d.download_path)):
                    return d
            except ValueError:
                continue
        return None

    def _exclude_words(self, dir_conf: Optional[TransferDirConf]) -> List[str]:
        words = list(self.conf.engine.exclude_words or [])
        if dir_conf:
            words += list(dir_conf.exclude_words or [])
        return words

    def _is_processing(self, hash_: str) -> bool:
        with self._processing_lock:
            return hash_ in self._processing

    def _set_processing(self, hash_: str, value: bool) -> None:
        with self._processing_lock:
            if value:
                self._processing.add(hash_)
            else:
                self._processing.discard(hash_)

    def status(self) -> dict:
        with self._processing_lock:
            processing = list(self._processing)
        return {
            "processing": processing,
            "recent": self._recent[-20:],
            "downloaders": list(self.downloaders.keys()),
        }

    # ------------------------------------------------------------ 主流程

    def organize(
        self,
        source: Path,
        download_hash: str = None,
        downloader: str = None,
        preview: bool = False,
        force: bool = False,
        transfer_type: str = None,
        target_path: Path = None,
    ) -> OrganizeResult:
        """整理一个文件或目录。返回结果对象(不会抛异常)。"""
        source = Path(source)
        if not source.exists():
            return OrganizeResult(all_success=False, message=f"源路径不存在: {source}")

        # 1. 目录配置
        dir_conf: Optional[TransferDirConf] = None
        if target_path:
            dir_conf = TransferDirConf(
                name="manual",
                download_path=str(source.parent) if source.is_file() else str(source),
                library_path=str(target_path),
                transfer_type=transfer_type or "copy",
                media_type="all",
                renaming=True,
                monitor=False,
                overwrite_mode=self.conf.engine.default_overwrite,
            )
        else:
            dir_conf = self.match_dir(source)
            if not dir_conf:
                logger.info(f"{source} 未匹配到下载目录配置,跳过")
                return OrganizeResult(total=0, message="未匹配到下载目录配置")
            if transfer_type:
                dir_conf.transfer_type = transfer_type

        transfer_type_eff = dir_conf.transfer_type
        overwrite_eff = dir_conf.overwrite_mode or self.conf.engine.default_overwrite

        # 2. 规划
        items = plan(source, self.conf.engine, self._exclude_words(dir_conf))
        if not items:
            return OrganizeResult(message="没有找到可整理的媒体文件")

        # 3. 媒体类型校验(配置了 movie/tv 时)
        items = [it for it in items if self._type_matches(it, dir_conf)]
        if not items:
            return OrganizeResult(message=f"没有符合 {dir_conf.media_type} 类型的文件")

        # 4. 识别 + 目标路径计算
        dest_root = Path(dir_conf.library_path)
        if dir_conf.category:
            dest_root = dest_root / dir_conf.category
        self._main_dest_cache = {}

        planned: List[dict] = []   # {item, dest, transfer_type, overwrite}
        skipped = 0
        for item in items:
            dest = self._build_dest(item, dest_root, dir_conf)
            if dest is None:
                skipped += 1
                continue
            # 5. 幂等检查(force 跳过)
            if not force and not preview:
                if self.store.get_success_by_source(str(item.source)):
                    skipped += 1
                    continue
            planned.append({"item": item, "dest": dest,
                            "transfer_type": transfer_type_eff,
                            "overwrite": overwrite_eff})

        if not planned:
            return OrganizeResult(total=len(items), skipped=skipped,
                                  message="全部文件已整理过或无法计算目标路径")

        # 6. 执行(preview 只规划不落盘)
        results: List[dict] = []
        success = failed = 0
        if preview:
            for p in planned:
                item, dest = p["item"], p["dest"]
                results.append({"source": str(item.source), "target": str(dest),
                                "success": True, "message": "preview", "kind": item.kind})
                success += 1
        else:
            workers = max(1, self.conf.engine.threads)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._exec_one, p): p for p in planned}
                for fut, p in futures.items():
                    item, dest = p["item"], p["dest"]
                    ok, msg = fut.result()
                    self._write_history(item, dest, p["transfer_type"],
                                        download_hash, downloader, ok, msg)
                    results.append({"source": str(item.source), "target": str(dest),
                                    "success": ok, "message": msg, "kind": item.kind})
                    if ok:
                        success += 1
                    else:
                        failed += 1

        all_success = failed == 0
        result = OrganizeResult(total=len(planned), success=success, failed=failed,
                                skipped=skipped, all_success=all_success,
                                preview=preview, items=results)

        # 7. 收尾
        if not preview:
            self._finalize(source, dir_conf, download_hash, downloader, all_success)

        self._recent.append({
            "source": str(source), "hash": download_hash, "downloader": downloader,
            "total": result.total, "success": result.success, "failed": result.failed,
            "all_success": all_success, "time": _now(),
        })
        logger.info(f"整理完成: {source} 共{result.total}个,成功{result.success},"
                    f"失败{result.failed},跳过{result.skipped}"
                    + (",已打标签" if all_success and download_hash and not preview else ""))
        return result

    # ------------------------------------------------------------ 内部

    def _type_matches(self, item: PlanItem, dir_conf: TransferDirConf) -> bool:
        """类型校验:附加文件跟随其主视频判定。"""
        if dir_conf.media_type == "all":
            return True
        ref = item
        if item.is_extra and item.related:
            ref = item.related
        is_tv = ref.meta.is_tv if ref.meta else False
        if dir_conf.media_type == "tv":
            return is_tv
        return not is_tv

    def _build_dest(self, item: PlanItem, dest_root: Path,
                    dir_conf: TransferDirConf) -> Optional[Path]:
        """计算单个文件的目标路径。"""
        if item.is_extra:
            related = item.related
            if related:
                related_dest = self._main_dest_cache.get(id(related))
                if related_dest is None:
                    related_dest = self._build_main_dest(related, dest_root, dir_conf)
                    if related_dest is not None:
                        self._main_dest_cache[id(related)] = related_dest
                if related_dest is None:
                    return None
                if not dir_conf.renaming:
                    return dest_root / item.source.name
                lang = ""
                if item.kind == "subtitle" and self.conf.engine.rename.subtitle_lang_tag:
                    lang = subtitle_lang_tag(item.source.name)
                return related_dest.parent / f"{related_dest.stem}{lang}{item.source.suffix.lower()}"
            # 孤儿附加文件:独立解析后按模板渲染
            meta = item.meta or parse_filename(item.source.name)
            if not meta.title:
                return None
            return self._render_main(meta, None, dest_root, dir_conf, item.source.name)

        return self._build_main_dest(item, dest_root, dir_conf)

    def _build_main_dest(self, item: PlanItem, dest_root: Path,
                         dir_conf: TransferDirConf) -> Optional[Path]:
        meta = item.meta
        media = self.recognizer.recognize(meta) if meta else None
        # 识别结果与配置类型不一致时跳过(如配置 movie 但 TMDB 识别为剧集)
        if dir_conf.media_type in ("movie", "tv") and media and media.media_type:
            if dir_conf.media_type != media.media_type:
                return None
        return self._render_main(meta, media, dest_root, dir_conf, item.source.name)

    def _render_main(self, meta, media, dest_root: Path, dir_conf: TransferDirConf,
                     source_name: str) -> Path:
        if not dir_conf.renaming:
            return dest_root / source_name
        template = (self.conf.engine.rename.tv if meta.is_tv
                    else self.conf.engine.rename.movie)
        rel = render_path(meta, media, template, self.conf.engine.rename.s0_alias)
        if not rel:
            return None
        return dest_root / rel

    def _exec_one(self, p: dict):
        item, dest = p["item"], p["dest"]
        tt, ow = p["transfer_type"], p["overwrite"]
        try:
            if item.source.is_dir():
                result = executor.transfer_dir(item.source, dest, tt, ow)
            else:
                result = executor.transfer_file(
                    item.source, dest, tt, ow, is_extra=item.is_extra)
            return result.success, result.message
        except Exception as e:  # noqa: BLE001
            logger.exception(f"整理异常: {item.source}")
            return False, str(e)

    def _write_history(self, item, dest, transfer_type, hash_, downloader, ok, msg) -> None:
        try:
            self.store.add(
                source_path=str(item.source),
                status="success" if ok else "failed",
                download_hash=hash_,
                downloader=downloader,
                target_path=str(dest) if ok else None,
                meta=item.meta.to_dict() if item.meta else None,
                transfer_type=transfer_type,
                message=None if ok else (msg or "")[:500],
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"写历史失败: {e}")

    def _finalize(self, source: Path, dir_conf: TransferDirConf, download_hash: str,
                  downloader: str, all_success: bool) -> None:
        """全部成功后的收尾:打标签、清理源空目录。"""
        # 打标签
        if all_success and download_hash and downloader:
            adapter = self.downloaders.get(downloader)
            if adapter:
                if adapter.add_tag(download_hash):
                    logger.info(f"已打整理标签: {download_hash}")
                else:
                    logger.warning(f"打标签失败: {download_hash}")
        # move 模式清理源空目录
        if all_success and dir_conf.transfer_type == "move" \
                and self.conf.engine.delete_empty_source_dirs:
            stop_at = Path(dir_conf.download_path) if dir_conf.download_path else None
            local.cleanup_empty_dirs(source, stop_at=stop_at)

    # ------------------------------------------------------------ 触发入口

    def on_webhook(self, payload: dict, downloader: str = None) -> Optional[OrganizeResult]:
        """处理下载器 webhook;非完成事件或已处理返回 None。"""
        adapter = self._resolve_adapter(downloader)
        if not adapter:
            return None
        event: Optional[WebhookEvent] = adapter.parse_webhook(payload)
        if not event:
            return None
        logger.info(f"收到下载完成事件: {event.name} [{event.hash}]")

        if self._is_processing(event.hash):
            logger.info(f"{event.hash} 正在整理中,跳过")
            return None
        # 已全部成功过且无失败记录 → 跳过(webhook 重复推送)
        if self.store.success_count_by_hash(event.hash) > 0 \
                and self.store.fail_count_by_hash(event.hash) == 0:
            logger.info(f"{event.hash} 已整理完成,跳过")
            return None

        content = Path(event.content_path)
        if not content.exists():
            # qB contentPath 可能指向单文件
            logger.warning(f"webhook 内容路径不存在: {content}")
            return None
        self._set_processing(event.hash, True)
        try:
            return self.organize(
                source=content,
                download_hash=event.hash,
                downloader=adapter.name,
            )
        finally:
            self._set_processing(event.hash, False)

    def poll_once(self, downloader: str = None) -> dict:
        """对账一轮:整理所有「已完成且未打标签」的任务。返回统计。"""
        adapter = self._resolve_adapter(downloader)
        if not adapter:
            return {"error": "下载器不存在"}
        torrents = adapter.list_finished()
        scanned = len(torrents)
        organized = skipped = failed = 0
        for t in torrents:
            if adapter.has_tag(t, self._tag_of(adapter)):
                continue
            if self._is_processing(t.hash):
                continue
            if self.store.success_count_by_hash(t.hash) > 0 \
                    and self.store.fail_count_by_hash(t.hash) == 0:
                skipped += 1
                continue
            self._set_processing(t.hash, True)
            try:
                result = self.organize(
                    source=t.content_path,
                    download_hash=t.hash,
                    downloader=adapter.name,
                )
                if result.total == 0:
                    skipped += 1
                elif result.all_success:
                    organized += 1
                else:
                    failed += 1
            finally:
                self._set_processing(t.hash, False)
        logger.info(f"轮询[{adapter.name}]完成: 扫描{scanned}, 整理{organized}, "
                    f"跳过{skipped}, 失败{failed}")
        return {"scanned": scanned, "organized": organized, "skipped": skipped,
                "failed": failed}

    def redo(self, history_id: int) -> tuple[bool, str, Optional[OrganizeResult]]:
        """按历史记录重新整理。"""
        rec = self.store.get_by_id(history_id)
        if not rec:
            return False, f"历史记录不存在: {history_id}", None
        source = Path(rec.source_path)
        if not source.exists():
            return False, f"源文件不存在: {source}", None
        self.store.delete(history_id)
        result = self.organize(
            source=source,
            download_hash=rec.download_hash,
            downloader=rec.downloader,
            force=True,
        )
        return result.all_success, result.message, result

    # ------------------------------------------------------------ 私有

    def _resolve_adapter(self, downloader: str = None) -> Optional[DownloaderAdapter]:
        if not self.downloaders:
            return None
        if downloader:
            return self.downloaders.get(downloader)
        return next(iter(self.downloaders.values()))

    def _tag_of(self, adapter: DownloaderAdapter) -> str:
        for dl in self.conf.downloaders:
            if dl.name == adapter.name:
                return dl.tag
        return "已整理"

    def close(self):
        self.recognizer.close()
        for adapter in self.downloaders.values():
            if hasattr(adapter, "_client") and adapter._client:
                adapter._client.close()


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")

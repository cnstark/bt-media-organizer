"""整理规划:收集候选文件 → 过滤 → 排序(参照 MP do_transfer 的规划逻辑)。

规划产物 PlanItem:
  - main    主视频(含蓝光原盘目录)
  - subtitle / audio  附加文件(归属 related 主视频)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import EngineConf
from ..parse.filename import ParsedMeta, normalize_stem, parse_filename, strip_lang_tag

logger = logging.getLogger("lite-organizer.planner")

# 隐藏目录 / 回收站 / 群晖 @eaDir
_HIDDEN_RE = re.compile(r"(^|/)(\.|@Recycle|#recycle|@eaDir)(/|$)")
# 蓝光原盘 BDMV/STREAM
_BLURAY_SUB_RE = re.compile(r"BDMV[/\\]STREAM", re.I)


@dataclass
class PlanItem:
    source: Path
    kind: str                        # main / subtitle / audio / bluray
    meta: Optional[ParsedMeta] = None
    related: Optional["PlanItem"] = None

    @property
    def is_extra(self) -> bool:
        return self.kind in ("subtitle", "audio")


# ---------------------------------------------------------------- 判定

def is_bluray_dir(path: Path) -> bool:
    """目录是否为蓝光原盘根目录(含 BDMV 子目录)。"""
    return path.is_dir() and (path / "BDMV").exists()


def is_bluray_sub(path: Path) -> bool:
    """路径是否为蓝光原盘内部文件(BDMV/STREAM)。"""
    return bool(_BLURAY_SUB_RE.search(path.as_posix()))


def _is_hidden(path: Path) -> bool:
    return bool(_HIDDEN_RE.search(path.as_posix()))


def _is_blocked(path: Path, exclude_words: List[str]) -> bool:
    if not exclude_words:
        return False
    p = path.as_posix()
    return any(w and w in p for w in exclude_words)


def _allowed_ext(path: Path, conf: EngineConf) -> bool:
    return path.suffix.lower() in conf.all_exts


# ---------------------------------------------------------------- 规划

def plan(source: Path, conf: EngineConf, exclude_words: List[str]) -> List[PlanItem]:
    """
    从源路径生成整理规划:
      - 单文件:自身 + 同目录附加文件(读一次父目录)
      - 目录:蓝光原盘整体一项;否则递归展开
      - 过滤:临时后缀 / 隐藏 / 屏蔽词 / 大小 / 扩展名
      - 排序:主视频优先,同名附加文件跟随,其余最后
    """
    if not source.exists():
        logger.warning(f"源路径不存在: {source}")
        return []

    # 蓝光原盘内部文件(BDMV/STREAM/...)→ 提升为原盘根目录整体整理
    if is_bluray_sub(source):
        for p in source.parents:
            if p.name == "BDMV":
                bluray_root = p.parent
                if is_bluray_dir(bluray_root):
                    meta = parse_filename(bluray_root.name)
                    if meta.title:
                        return [PlanItem(source=bluray_root, kind="bluray", meta=meta)]
                break
        return []

    if source.is_file():
        if conf.is_tmp(source) or _is_hidden(source) or _is_blocked(source, exclude_words):
            return []
        main_items, extras = _classify_file(source, conf, exclude_words)
        if main_items:
            # 读一次父目录收集附加文件
            _, sibling_extras = _scan_dir(source.parent, conf, exclude_words,
                                          main_only=False)
            return _attach_extras(sibling_extras, [main_items[0]])
        if extras:
            return extras
        return []

    # 目录
    if is_bluray_dir(source):
        meta = parse_filename(source.name)
        if meta.title:
            return [PlanItem(source=source, kind="bluray", meta=meta)]
        logger.warning(f"蓝光原盘目录名无法解析: {source.name}")
        return []

    main_items, extras = _scan_dir(source, conf, exclude_words, main_only=False)
    return _attach_extras(extras, main_items)


def _classify_file(
    path: Path, conf: EngineConf, exclude_words: List[str]
) -> Tuple[List[PlanItem], List[PlanItem]]:
    """对单个文件分类:主视频 / 附加文件。"""
    if not _allowed_ext(path, conf):
        return [], []
    if conf.is_tmp(path) or _is_hidden(path) or _is_blocked(path, exclude_words):
        return [], []
    ext = path.suffix.lower()
    if ext in conf.subtitle_exts:
        return [], [PlanItem(source=path, kind="subtitle")]
    if ext in conf.audio_exts:
        return [], [PlanItem(source=path, kind="audio")]
    if ext in conf.media_exts:
        # 最小大小仅对主视频生效(字幕/音频不受限)
        if conf.min_filesize and path.stat().st_size < conf.min_filesize * 1024 * 1024:
            return [], []
        meta = parse_filename(path.name)
        if not meta.title:
            return [], []
        return [PlanItem(source=path, kind="main", meta=meta)], []
    return [], []


def _scan_dir(
    path: Path,
    conf: EngineConf,
    exclude_words: List[str],
    main_only: bool,
) -> Tuple[List[PlanItem], List[PlanItem]]:
    """递归扫描目录。main_only=True 时跳过附加文件收集(性能优化)。"""
    mains: List[PlanItem] = []
    extras: List[PlanItem] = []
    for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
        if _is_hidden(child) or _is_blocked(child, exclude_words):
            continue
        if child.is_dir():
            if is_bluray_dir(child):
                meta = parse_filename(child.name)
                if meta.title:
                    mains.append(PlanItem(source=child, kind="bluray", meta=meta))
                continue
            if is_bluray_sub(child):
                continue
            m, e = _scan_dir(child, conf, exclude_words, main_only)
            mains += m
            extras += e
            continue
        if not child.is_file():
            continue
        m, e = _classify_file(child, conf, exclude_words)
        mains += m
        if not main_only:
            extras += e
    return mains, extras


def _attach_extras(extras: List[PlanItem], mains: List[PlanItem]) -> List[PlanItem]:
    """附加文件按目录归属主视频;未匹配的返回自身(由上层决定处理)。"""
    by_dir: dict = {}
    for main in mains:
        by_dir.setdefault(str(main.source.parent), []).append(main)

    matched: List[PlanItem] = []
    orphan: List[PlanItem] = []
    for extra in extras:
        main_list = by_dir.get(str(extra.source.parent), [])
        related = _match_main(extra.source, main_list)
        if related:
            extra.related = related
            matched.append(extra)
        else:
            orphan.append(extra)
    # 主视频按名称排序,附加文件跟在所属主视频后面
    result: List[PlanItem] = []
    for main in mains:
        result.append(main)
        result += [e for e in matched if e.related is main]
    # 未匹配的附加文件:尝试独立解析,有标题则追加末尾
    for extra in orphan:
        meta = parse_filename(extra.source.name)
        if meta.title:
            extra.meta = meta
            result.append(extra)
    return result


def _match_main(extra_path: Path, mains: List[PlanItem]) -> Optional[PlanItem]:
    """附加文件归属:解析后按 标题/年份/季/集 与主视频比对。"""
    extra_meta = parse_filename(extra_path.name)
    if not extra_meta.title:
        return None
    for main in mains:
        mm = main.meta
        if not mm or mm.title != extra_meta.title:
            continue
        # 双方都有年份时才校验(附加文件常缺年份)
        if mm.year and extra_meta.year and mm.year != extra_meta.year:
            continue
        # 剧集:季/集必须一致(电影双方均为 None,跳过)
        if mm.season != extra_meta.season or mm.begin_episode != extra_meta.begin_episode:
            continue
        return main
    return None

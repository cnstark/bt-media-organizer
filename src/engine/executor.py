"""单文件/目录转移执行 + 覆盖策略(参照 MP TransHandler.__transfer_file)。

覆盖决策:
  - 附加文件(字幕/音频):强制覆盖
  - 主文件:按 overwrite_mode(never/always/size/latest)
  - latest:删除目标目录中同季集同 Part 的其他视频文件
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..parse.filename import ParsedMeta, parse_filename
from ..storage import local

logger = logging.getLogger("ptpilot.executor")


@dataclass
class FileResult:
    success: bool
    message: str
    source: Path
    target: Optional[Path] = None


# ---------------------------------------------------------------- 单文件

def transfer_file(
    src: Path,
    dest: Path,
    transfer_type: str,
    overwrite_mode: str,
    is_extra: bool = False,
) -> FileResult:
    """转移单个文件,按覆盖策略处理目标已存在的情况。"""
    if not src.exists() or not src.is_file():
        return FileResult(False, f"源文件不存在: {src}", src)

    overwrite = False
    if dest.exists() or dest.is_symlink():
        if is_extra:
            overwrite = True  # 附加文件强制覆盖
        else:
            overwrite = _decide_overwrite(src, dest, overwrite_mode)
            if not overwrite:
                return FileResult(False, f"目标已存在且覆盖策略为 {overwrite_mode}: {dest}", src)
        # 覆盖前先删除旧文件(软链接目标指向不存在文件时同样处理)
        if dest.is_dir() and not dest.is_symlink():
            local.delete_file(dest)
        else:
            try:
                dest.unlink(missing_ok=True)
            except OSError as e:
                return FileResult(False, f"删除旧目标失败: {e}", src)

    try:
        local.ensure_dir(dest.parent)
    except OSError as e:
        return FileResult(False, f"创建目标目录失败: {e}", src)

    ok = _do_transfer(src, dest, transfer_type)
    if not ok:
        return FileResult(False, f"转移失败 [{transfer_type}]: {src} -> {dest}", src)
    return FileResult(True, "ok", src, dest)


def _decide_overwrite(src: Path, dest: Path, mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "latest":
        # 仅保留最新版本:删除同目录其他版本,随后覆盖
        delete_version_files(dest)
        return True
    if mode == "size":
        try:
            return dest.stat().st_size < src.stat().st_size
        except OSError:
            return False
    return False  # never 及其他


def delete_version_files(dest: Path) -> None:
    """删除目标目录中同季集同 Part 的其他视频文件(参照 MP __delete_version_files)。"""
    meta = parse_filename(dest.name)
    if not meta.season and meta.begin_episode is None:
        return
    try:
        for child in dest.parent.iterdir():
            if child == dest or not child.is_file():
                continue
            if child.suffix.lower() not in (".mkv", ".mp4", ".ts", ".avi", ".wmv", ".mov", ".m2ts", ".iso"):
                continue
            cm = parse_filename(child.name)
            if cm.season != meta.season or cm.begin_episode != meta.begin_episode:
                continue
            if meta.part and cm.part and cm.part != meta.part:
                continue
            logger.info(f"latest 模式删除旧版本: {child}")
            child.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"删除旧版本文件失败: {e}")


def _do_transfer(src: Path, dest: Path, transfer_type: str) -> bool:
    if transfer_type == "copy":
        return local.copy_file(src, dest)
    if transfer_type == "move":
        return local.move_file(src, dest)
    if transfer_type == "hardlink":
        return local.hardlink(src, dest)
    if transfer_type == "softlink":
        return local.softlink(src, dest)
    logger.error(f"不支持的整理方式: {transfer_type}")
    return False


# ---------------------------------------------------------------- 目录(蓝光原盘)

def transfer_dir(
    src: Path,
    dest: Path,
    transfer_type: str,
    overwrite_mode: str,
) -> FileResult:
    """递归转移目录内全部文件,保持目录结构(用于蓝光原盘等)。"""
    if not src.exists() or not src.is_dir():
        return FileResult(False, f"源目录不存在: {src}", src)
    try:
        local.ensure_dir(dest)
    except OSError as e:
        return FileResult(False, f"创建目标目录失败: {e}", src)

    moved_files = 0
    for child in sorted(src.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir():
            if child.name.lower() == "bdmv" and transfer_type in ("move",):
                # 整个 BDMV 目录一次转移,减少跨盘 IO
                sub = transfer_dir(child, dest / child.name, transfer_type, overwrite_mode)
            else:
                sub = transfer_dir(child, dest / child.name, transfer_type, overwrite_mode)
            if not sub.success:
                return sub
            moved_files += 1
        elif child.is_file():
            result = transfer_file(child, dest / child.name, transfer_type, overwrite_mode, is_extra=False)
            if not result.success:
                return result
            moved_files += 1

    if transfer_type == "move":
        local.cleanup_empty_dirs(src)
    return FileResult(True, f"目录转移完成,共 {moved_files} 个文件", src, dest)

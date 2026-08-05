"""本地文件系统操作:copy / move / hardlink / softlink / 删除 / 空目录清理。

实现参照 MoviePilot `app/modules/filemanager/storages/local.py` 与
`app/utils/system.py`(硬链接 .mp 临时名中转保证原子性)。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def ensure_dir(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)


def is_same_disk(a: Path, b: Path) -> bool:
    """判断两个路径是否在同一文件系统(按 st_dev)。"""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _copy_with_target_permissions(src: Path, dest: Path) -> None:
    """复制内容与时间戳,不覆盖目标目录赋予的权限(参照 MP)。"""
    st = src.stat()
    shutil.copyfile(src, dest)
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))


def copy_file(src: Path, dest: Path) -> bool:
    try:
        _copy_with_target_permissions(src, dest)
        return True
    except OSError as e:
        return False


def move_file(src: Path, dest: Path) -> bool:
    """同盘 rename;跨盘复制+删源。"""
    try:
        if src == dest:
            return True
        if is_same_disk(src, dest.parent if dest.parent.exists() else dest.parent.parent):
            os.rename(src, dest)
        else:
            _copy_with_target_permissions(src, dest)
            src.unlink()
        return True
    except OSError:
        return False


def hardlink(src: Path, dest: Path) -> bool:
    """
    硬链接:先建 dest.mp 临时名再 rename,避免目标已存在时报错(参照 MP system.py)。
    """
    try:
        tmp_path = dest.with_suffix(dest.suffix + ".mp")
        if tmp_path.exists() or tmp_path.is_symlink():
            tmp_path.unlink()
        tmp_path.hardlink_to(src)
        os.replace(tmp_path, dest)
        return True
    except OSError:
        return False


def softlink(src: Path, dest: Path) -> bool:
    try:
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        dest.symlink_to(src)
        return True
    except OSError:
        return False


def delete_file(path: Path) -> bool:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def cleanup_empty_dirs(src_root: Path, stop_at: Optional[Path] = None) -> None:
    """
    从下往上删除空目录,直到 stop_at(不含)或遇到非空目录。
    用于 move 模式整理完成后的源目录清理(参照 MP __default_callback)。
    """
    if not src_root.exists():
        return
    current = src_root if src_root.is_dir() else src_root.parent
    while True:
        if stop_at is not None and current == stop_at:
            break
        if not current.exists() or current == current.parent:
            break
        try:
            if any(current.iterdir()):
                break
            current.rmdir()
        except OSError:
            break
        current = current.parent

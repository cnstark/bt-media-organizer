"""最小 bencode 编解码 + 种子/快传文件工具(零依赖)。

用途:
- .torrent 解析:info_hash 计算(必须取 info 原始字节切片,不能重编码)、announce 提取/修补
- qB .fastresume 解析:trackers[0][0] 提取
"""
from __future__ import annotations

import hashlib
from typing import Any, List, Optional, Tuple

# ---------------------------------------------------------------- 解码


def _parse(data: bytes, pos: int) -> Tuple[Any, int]:
    """解析一个值,返回 (value, next_pos)。dict 返回 {bytes: value}。"""
    c = data[pos:pos + 1]
    if c == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if c in b"0123456789":
        colon = data.index(b":", pos)
        n = int(data[pos:colon])
        start = colon + 1
        return data[start:start + n], start + n
    if c == b"l":
        pos += 1
        items: List[Any] = []
        while data[pos:pos + 1] != b"e":
            v, pos = _parse(data, pos)
            items.append(v)
        return items, pos + 1
    if c == b"d":
        pos += 1
        items: List[Tuple[bytes, Any]] = []
        while data[pos:pos + 1] != b"e":
            k, pos = _parse(data, pos)
            if not isinstance(k, bytes):
                raise ValueError("bencode dict key 不是 bytes")
            v, pos = _parse(data, pos)
            items.append((k, v))
        return dict(items), pos + 1
    raise ValueError(f"bencode 解析失败,位置 {pos}: {c!r}")


def decode(data: bytes) -> Any:
    """解码整个 bencode 数据。"""
    value, pos = _parse(data, 0)
    if pos != len(data):
        raise ValueError(f"bencode 尾部有多余数据: {pos} != {len(data)}")
    return value


def info_dict_raw(data: bytes) -> bytes:
    """提取顶层 dict 中 info 键值的原始字节切片(计算 infohash 用)。"""
    if not data.startswith(b"d"):
        raise ValueError("不是 dict 开头的 bencode 数据")
    pos = 1
    while data[pos:pos + 1] != b"e":
        k, pos = _parse(data, pos)
        if not isinstance(k, bytes):
            raise ValueError("顶层 dict key 不是 bytes")
        start = pos
        _, pos = _parse(data, pos)
        if k == b"info":
            return data[start:pos]
    raise ValueError("缺少 info 键")


def info_hash(torrent_bytes: bytes) -> str:
    """种子文件的 infohash(v1, hex 小写)。"""
    return hashlib.sha1(info_dict_raw(torrent_bytes)).hexdigest()


def extract_announce(torrent_bytes: bytes) -> Optional[str]:
    """取 announce(找不到返回 None)。"""
    try:
        d = decode(torrent_bytes)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    value = d.get(b"announce")
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return None


# ---------------------------------------------------------------- 编码


def _encode(obj: Any) -> bytes:
    if isinstance(obj, dict):
        parts = [b"d"]
        for k in sorted(obj, key=lambda x: x if isinstance(x, bytes) else str(x).encode()):
            kb = k if isinstance(k, bytes) else str(k).encode("utf-8")
            parts.append(_encode(kb))
            parts.append(_encode(obj[k]))
        parts.append(b"e")
        return b"".join(parts)
    if isinstance(obj, (list, tuple)):
        return b"l" + b"".join(_encode(v) for v in obj) + b"e"
    if isinstance(obj, int):
        return b"i" + str(obj).encode() + b"e"
    if isinstance(obj, bytes):
        return str(len(obj)).encode() + b":" + obj
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        return str(len(b)).encode() + b":" + b
    raise TypeError(f"不支持的类型: {type(obj)}")


def encode(obj: Any) -> bytes:
    return _encode(obj)


def patch_announce(torrent_bytes: bytes, announce: str) -> bytes:
    """给 .torrent 补 announce(缺失/为空时),返回新的种子字节。"""
    d = decode(torrent_bytes)
    if not isinstance(d, dict):
        raise ValueError("种子结构非法")
    current = d.get(b"announce")
    if current and isinstance(current, bytes) and current:
        return torrent_bytes  # 已有 announce,原样返回
    d[b"announce"] = announce.encode("utf-8")
    return encode(d)


def fastresume_tracker(fastresume_bytes: bytes) -> Optional[str]:
    """从 qB .fastresume 提取 trackers[0][0](补 announce 用)。"""
    try:
        d = decode(fastresume_bytes)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    trackers = d.get(b"trackers")
    if not isinstance(trackers, list) or not trackers:
        return None
    first = trackers[0]
    if isinstance(first, list) and first and isinstance(first[0], bytes):
        return first[0].decode("utf-8", "replace")
    return None

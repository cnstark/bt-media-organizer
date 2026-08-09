"""Transmission 适配器(RPC over HTTP JSON,零额外依赖 httpx)。

要点:
- X-Transmission-Session-Id 头;409 Conflict 时取响应头新 session-id 重试一次
- 新老协议双支持:首次调用用老协议 session-get 探测版本,>=4.1 切 JSON-RPC 2.0
  (snake_case 方法/字段),否则用老协议(kebab-case/camelCase);新协议被拒(400)自动回退老协议
- torrent-add 用 metainfo=base64(元数据)或 filename=URL
- 做种状态 status==6;percentDone>=1 视为完成
- 删除只删种子(delete-local-data=false)
"""
from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple, Union

import httpx

from ..config import DownloaderConf
from .base import DownloaderAdapter, TorrentInfo

logger = logging.getLogger("ptpilot.transmission")

# transmission 状态码 → 文本
_STATUS_TEXT = {
    0: "stopped", 1: "check-wait", 2: "checking", 3: "download-wait",
    4: "downloading", 5: "seed-wait", 6: "seeding",
}

# 老协议方法名(kebab-case)→ 新协议(JSON-RPC 2.0,snake_case)
_PROTOCOL_METHODS = {
    "torrent-get": "torrent_get",
    "torrent-set": "torrent_set",
    "torrent-add": "torrent_add",
    "torrent-remove": "torrent_remove",
    "torrent-verify": "torrent_verify",
    "session-get": "session_get",
}

# 老协议(camelCase)→ 新协议(snake_case)字段/参数名映射
_FIELD_MAP = {
    "hashString": "hash_string",
    "downloadDir": "download_dir",
    "percentDone": "percent_done",
    "torrentFile": "torrent_file",
    "totalSize": "total_size",
}
_ARG_MAP = {
    "download-dir": "download_dir",
    "delete-local-data": "delete_local_data",
}


def _pick(item: dict, *names):
    """按顺序取第一个存在的键(兼容新老协议字段名)。"""
    for n in names:
        if n in item:
            return item[n]
    return None


class TransmissionAdapter(DownloaderAdapter):
    def __init__(self, conf: DownloaderConf):
        self.name = conf.name
        self.conf = conf
        self._client = httpx.Client(
            base_url=conf.url.rstrip("/"), timeout=30.0, follow_redirects=True
        )
        self._session_id = ""
        self._probed = False      # 是否已探测协议
        self._new_api = False     # True=JSON-RPC 2.0(4.1+);False=老协议
        self._version = ""        # 探测到的服务端版本

    # ---------------- 私有 ----------------

    def _auth(self):
        """Basic Auth tuple(httpx 要求 tuple 或 Auth 实例)。"""
        if self.conf.username:
            return (self.conf.username, self.conf.password or "")
        return None

    def _post(self, payload: dict) -> Tuple[int, dict]:
        """POST RPC;处理 409 会话重试;返回 (http_code, json)。401 直接抛错。"""
        headers = {"Content-Type": "application/json"}
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id
        resp = self._client.post(
            "/transmission/rpc", json=payload, headers=headers, auth=self._auth()
        )
        if resp.status_code == 409:
            sid = resp.headers.get("X-Transmission-Session-Id", "")
            if not sid:
                raise RuntimeError("TR 409 但响应头缺少 X-Transmission-Session-Id")
            self._session_id = sid
            headers["X-Transmission-Session-Id"] = sid
            resp = self._client.post(
                "/transmission/rpc", json=payload, headers=headers, auth=self._auth()
            )
        if resp.status_code == 401:
            raise RuntimeError("Transmission 认证失败(401)")
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            data = {}
        return resp.status_code, data

    def _ensure_probe(self) -> None:
        if not self._probed:
            self._probe()

    def _probe(self) -> None:
        """老协议 session-get 探测版本;>=4.1 切 JSON-RPC 2.0,新协议冒烟失败回退老协议。"""
        self._probed = True
        code, data = self._post({"method": "session-get", "arguments": {"fields": ["version"]}})
        if code == 200:
            self._version = str((data.get("arguments") or {}).get("version") or "")
        m = re.match(r"(\d+)\.(\d+)", self._version)
        self._new_api = bool(m) and (int(m.group(1)), int(m.group(2))) >= (4, 1)
        if self._new_api:
            try:
                code2, _ = self._post({"jsonrpc": "2.0", "method": "session_get",
                                       "params": {"fields": ["version"]}, "id": 1})
                if code2 != 200:
                    self._new_api = False
            except Exception:  # noqa: BLE001
                self._new_api = False  # 新协议冒烟失败,回退老协议

    def _request(self, method: str, args: dict = None) -> dict:
        """发送 RPC 请求(按探测结果选新老协议),失败抛异常,由调用方捕获。"""
        self._ensure_probe()
        args = dict(args or {})
        is_new = self._new_api
        if is_new:
            payload = {
                "jsonrpc": "2.0",
                "method": _PROTOCOL_METHODS.get(method, method.replace("-", "_")),
                "params": {_ARG_MAP.get(k, k): v for k, v in args.items()},
                "id": 1,
            }
        else:
            payload = {"method": method, "arguments": args}
        code, data = self._post(payload)
        if is_new and code == 400:  # 新协议被拒 → 回退老协议重试一次
            self._new_api = False
            payload = {"method": method, "arguments": args}
            code, data = self._post(payload)
        if code != 200:
            raise RuntimeError(f"TR RPC HTTP {code}")
        if is_new:
            if data.get("error"):
                err = data["error"]
                raise RuntimeError(f"TR RPC 失败: {err.get('message') or err}")
            return data.get("result") or {}
        if data.get("result") != "success":
            raise RuntimeError(f"TR RPC 失败: {data.get('result')}")
        return data.get("arguments") or {}

    def _torrent_get(self, fields: List[str], ids: Union[str, int, List, None] = None) -> List[dict]:
        self._ensure_probe()
        if self._new_api:
            fields = [_FIELD_MAP.get(f, f) for f in fields]
        args = {"fields": fields}
        if ids is not None:
            args["ids"] = ids
        return self._request("torrent-get", args).get("torrents") or []

    @staticmethod
    def _to_info(item: dict) -> TorrentInfo:
        status = int(_pick(item, "status", "status") or 0)
        name = _pick(item, "name", "name") or ""
        download_dir = _pick(item, "downloadDir", "download_dir") or ""
        torrent_file = _pick(item, "torrentFile", "torrent_file") or ""
        trackers = _pick(item, "trackers", "trackers") or []
        tracker = ""
        for t in trackers:
            announce = t.get("announce") or ""
            if announce:
                tracker = announce
                break
        return TorrentInfo(
            hash=(_pick(item, "hashString", "hash_string") or "").lower(),
            name=name,
            save_path=Path(download_dir),
            content_path=(Path(download_dir) / name) if download_dir and name else Path(download_dir),
            tags=list(_pick(item, "labels", "labels") or []),
            size=int(_pick(item, "totalSize", "total_size") or 0),
            state=_STATUS_TEXT.get(status, str(status)),
            tracker=tracker,
            torrent_file=torrent_file,
            done=float(_pick(item, "percentDone", "percent_done") or 0.0) >= 1.0,
            seeding=status == 6,
        )

    # ---------------- 协议实现 ----------------

    def list_finished(self) -> List[TorrentInfo]:
        return [t for t in self.list_torrents("completed") if t.done]

    def list_torrents(self, state: str = "all") -> List[TorrentInfo]:
        fields = ["id", "hashString", "name", "downloadDir", "totalSize", "status",
                  "percentDone", "labels", "trackers", "torrentFile"]
        items = self._torrent_get(fields)
        torrents = [self._to_info(item) for item in items]
        if state == "seeding":
            return [t for t in torrents if t.seeding]
        if state == "completed":
            return [t for t in torrents if t.done]
        return torrents

    def get_torrent_file(self, hash: str) -> Optional[bytes]:
        """TR RPC 的 torrentFile 指向磁盘路径,直接读(跨容器场景可能不可达)。"""
        items = self._torrent_get(["hashString", "torrentFile"], ids=[hash])
        if not items:
            return None
        path = _pick(items[0], "torrentFile", "torrent_file") or ""
        if not path:
            return None
        try:
            return Path(path).read_bytes()
        except OSError as e:
            logger.warning(f"[{self.name}] 读取种子文件失败 {path}: {e}")
            return None

    def get_torrent_files(self, hash: str) -> List[tuple]:
        """种子文件列表(RPC files 字段,跨主机可用)。"""
        try:
            items = self._torrent_get(["hashString", "files"], ids=[hash])
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.name}] 获取种子文件列表失败: {e}")
            return []
        if not items:
            return []
        files = items[0].get("files") or []
        return [(f.get("name") or "", int(f.get("length") or 0)) for f in files]

    def add_torrent(self, data: Union[bytes, str], save_path: str, *,
                    paused: bool, category: str = "", tags: Optional[List[str]] = None,
                    skip_checking: bool = False) -> Tuple[bool, str]:
        args = {"download-dir": save_path, "paused": bool(paused)}
        labels = []
        if category:
            labels.append(category)
        if tags:
            labels.extend(tags)
        if labels:
            args["labels"] = labels
        try:
            if isinstance(data, bytes):
                args["metainfo"] = base64.b64encode(data).decode("ascii")
            else:
                args["filename"] = data
            self._request("torrent-add", args)
            return True, "ok"
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.name}] torrent-add 失败: {e}")
            return False, str(e)

    def add_tag(self, hash: str) -> bool:
        """整理完成标签:labels 追加 conf.tag。"""
        if not self.conf.tag:
            return True
        try:
            items = self._torrent_get(["hashString", "labels"], ids=[hash])
            if not items:
                return False
            labels = list(items[0].get("labels") or [])
            if self.conf.tag not in labels:
                labels.append(self.conf.tag)
            self._request("torrent-set", {"ids": [hash], "labels": labels})
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.name}] 打标签失败: {e}")
            return False

    def delete_torrent(self, hash: str, delete_files: bool = True) -> bool:
        try:
            self._request("torrent-remove", {"ids": [hash], "delete-local-data": bool(delete_files)})
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.name}] torrent-remove 失败: {e}")
            return False

    def recheck(self, hash: str) -> bool:
        try:
            self._request("torrent-verify", {"ids": [hash]})
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.name}] torrent-verify 失败: {e}")
            return False

    def app_version(self) -> str:
        try:
            self._ensure_probe()
            return self._version
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.name}] session-get 失败: {e}")
            return ""

    def has_torrent(self, hash: str) -> bool:
        try:
            return bool(self._torrent_get(["hashString"], ids=[hash]))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.name}] has_torrent 失败: {e}")
            return False

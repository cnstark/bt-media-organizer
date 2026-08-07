"""Transmission 适配器(RPC over HTTP JSON,零额外依赖 httpx)。

要点:
- X-Transmission-Session-Id 头;409 Conflict 时取响应头新 session-id 重试一次
- torrent-add 用 metainfo=base64(元数据)或 filename=URL
- 做种状态 status==6;percentDone>=1 视为完成
- 删除只删种子(delete-local-data=false)
"""
from __future__ import annotations

import base64
import logging
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


class TransmissionAdapter(DownloaderAdapter):
    def __init__(self, conf: DownloaderConf):
        self.name = conf.name
        self.conf = conf
        self._client = httpx.Client(
            base_url=conf.url.rstrip("/"), timeout=30.0, follow_redirects=True
        )
        self._session_id = ""

    # ---------------- 私有 ----------------

    def _auth(self):
        """Basic Auth tuple(httpx 要求 tuple 或 Auth 实例)。"""
        if self.conf.username:
            return (self.conf.username, self.conf.password or "")
        return None

    def _request(self, method: str, args: dict = None) -> dict:
        """发送 RPC 请求,自动处理 409 会话。失败抛异常,由调用方捕获。"""
        payload = {"method": method, "arguments": args or {}}
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
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != "success":
            raise RuntimeError(f"TR RPC 失败: {data.get('result')}")
        return data.get("arguments") or {}

    def _torrent_get(self, fields: List[str], ids: Union[str, int, List, None] = None) -> List[dict]:
        args = {"fields": fields}
        if ids is not None:
            args["ids"] = ids
        return self._request("torrent-get", args).get("torrents") or []

    @staticmethod
    def _to_info(item: dict) -> TorrentInfo:
        status = int(item.get("status") or 0)
        name = item.get("name") or ""
        download_dir = item.get("downloadDir") or ""
        torrent_file = item.get("torrentFile") or ""
        trackers = item.get("trackers") or []
        tracker = ""
        for t in trackers:
            announce = t.get("announce") or ""
            if announce:
                tracker = announce
                break
        return TorrentInfo(
            hash=(item.get("hashString") or "").lower(),
            name=name,
            save_path=Path(download_dir),
            content_path=(Path(download_dir) / name) if download_dir and name else Path(download_dir),
            tags=list(item.get("labels") or []),
            size=int(item.get("totalSize") or 0),
            state=_STATUS_TEXT.get(status, str(status)),
            tracker=tracker,
            torrent_file=torrent_file,
            done=float(item.get("percentDone") or 0.0) >= 1.0,
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
        path = items[0].get("torrentFile") or ""
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
            return str(self._request("session-get", {"fields": ["version"]}).get("version") or "")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.name}] session-get 失败: {e}")
            return ""

    def has_torrent(self, hash: str) -> bool:
        try:
            return bool(self._torrent_get(["hashString"], ids=[hash]))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[{self.name}] has_torrent 失败: {e}")
            return False

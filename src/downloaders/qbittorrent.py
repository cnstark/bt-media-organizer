"""qBittorrent 适配器(WebUI API v2,自研最小客户端,httpx)。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import httpx

from ..config import DownloaderConf
from .base import DownloaderAdapter, TorrentInfo, WebhookEvent

logger = logging.getLogger("lite-organizer.qbittorrent")

# qBittorrent webhook 报文事件类型(仅处理下载完成)
_FINISH_EVENTS = {"torrent_finished", "torrent_completed"}


class QBittorrentAdapter(DownloaderAdapter):
    def __init__(self, conf: DownloaderConf):
        self.name = conf.name
        self.conf = conf
        self._client = httpx.Client(
            base_url=conf.url.rstrip("/"), timeout=15.0, follow_redirects=True
        )
        self._logged_in = False

    # ---------------- 私有 ----------------

    def _login(self) -> bool:
        if self._logged_in:
            return True
        try:
            resp = self._client.post(
                "/api/v2/auth/login",
                data={"username": self.conf.username, "password": self.conf.password},
            )
            if resp.status_code == 200 and resp.text == "Ok.":
                self._logged_in = True
                return True
            logger.error(f"[{self.name}] qBittorrent 登录失败: {resp.status_code} {resp.text[:200]}")
        except httpx.HTTPError as e:
            logger.error(f"[{self.name}] qBittorrent 登录异常: {e}")
        self._logged_in = False
        return False

    def _get(self, path: str, **params) -> Optional[list | dict]:
        if not self._login():
            return None
        try:
            resp = self._client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error(f"[{self.name}] qBittorrent 请求失败 {path}: {e}")
            self._logged_in = False  # 会话可能失效,强制重登
            return None

    def _post(self, path: str, data: dict) -> bool:
        if not self._login():
            return False
        try:
            resp = self._client.post(path, data=data)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"[{self.name}] qBittorrent 请求失败 {path}: {e}")
            self._logged_in = False
            return False

    # ---------------- 协议实现 ----------------

    def list_finished(self) -> List[TorrentInfo]:
        data = self._get("/api/v2/torrents/info", filter="completed")
        if not isinstance(data, list):
            return []
        torrents: List[TorrentInfo] = []
        for item in data:
            try:
                content_path = Path(item.get("content_path") or "")
                if not content_path.exists():
                    logger.warning(f"[{self.name}] 任务 {item.get('name')} 内容路径不存在,跳过: {content_path}")
                    continue
                torrents.append(
                    TorrentInfo(
                        hash=item.get("hash", ""),
                        name=item.get("name", ""),
                        save_path=Path(item.get("save_path") or ""),
                        content_path=content_path,
                        category=item.get("category", "") or "",
                        tags=(item.get("tags") or "").split(),
                        size=int(item.get("size") or 0),
                        state=item.get("state", ""),
                    )
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{self.name}] 解析种子信息失败: {e}")
        return torrents

    def add_tag(self, hash: str) -> bool:
        if not self.conf.tag:
            return True
        return self._post("/api/v2/torrents/addTags", {"hashes": hash, "tags": self.conf.tag})

    def delete_torrent(self, hash: str, delete_files: bool = True) -> bool:
        return self._post(
            "/api/v2/torrents/delete", {"hashes": hash, "deleteFiles": "true" if delete_files else "false"}
        )

    def parse_webhook(self, payload: dict) -> Optional[WebhookEvent]:
        if not isinstance(payload, dict):
            return None
        # qBittorrent webhook 字段大小写兼容
        event = payload.get("event") or payload.get("Event") or ""
        if event not in _FINISH_EVENTS:
            return None
        hash_ = payload.get("hash") or payload.get("Hash") or ""
        name = payload.get("name") or payload.get("Name") or ""
        save_path = payload.get("savePath") or payload.get("SavePath") or ""
        content_path = payload.get("contentPath") or payload.get("ContentPath") or save_path
        if not hash_ or not content_path:
            logger.warning(f"[{self.name}] webhook 报文缺少 hash/contentPath: {payload}")
            return None
        return WebhookEvent(
            event=event,
            hash=hash_,
            name=name,
            save_path=Path(save_path),
            content_path=Path(content_path),
            downloader=self.name,
        )

"""qBittorrent 适配器(WebUI API v2,自研最小客户端,httpx)。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional, Tuple, Union

import httpx

from ..config import DownloaderConf
from .base import DownloaderAdapter, TorrentInfo

logger = logging.getLogger("bt-media-organizer.qbittorrent")

# 做种状态集(IYUU 语义):正在上传/做种中/做种暂停/排队校验上传/校验中上传/强制上传
_SEEDING_STATES = {"uploading", "stalledUP", "pausedUP", "queuedUP", "checkingUP", "forcedUP"}
# 完成状态(对账用)
_DONE_STATES = {"uploading", "stalledUP", "pausedUP", "queuedUP", "checkingUP", "forcedUP",
                "stoppedUP", "checkingResumeData", "moving"}


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
            # qB 5.x 对白名单/免登录来源的 login 请求返回 204 No Content(而非 200 "Ok."),
            # 此时无需会话即可访问 API,同样视为登录成功
            if resp.status_code == 204 or (
                resp.status_code == 200 and resp.text.strip() == "Ok."
            ):
                self._logged_in = True
                return True
            logger.error(
                f"[{self.name}] qBittorrent 登录失败: {resp.status_code} {resp.text[:200]}"
            )
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

    @staticmethod
    def _to_info(item: dict) -> TorrentInfo:
        state = item.get("state", "")
        return TorrentInfo(
            hash=item.get("hash", ""),
            name=item.get("name", ""),
            save_path=Path(item.get("save_path") or ""),
            content_path=Path(item.get("content_path") or ""),
            category=item.get("category", "") or "",
            tags=(item.get("tags") or "").split(),
            size=int(item.get("size") or 0),
            state=state,
            infohash_v1=item.get("infohash_v1") or "",
            done=state in _DONE_STATES,
            seeding=state in _SEEDING_STATES,
        )

    # ---------------- 整理模块(现状) ----------------

    def list_finished(self) -> List[TorrentInfo]:
        data = self._get("/api/v2/torrents/info", filter="completed")
        if not isinstance(data, list):
            return []
        torrents: List[TorrentInfo] = []
        for item in data:
            try:
                info = self._to_info(item)
                if not info.content_path.exists():
                    logger.warning(f"[{self.name}] 任务 {item.get('name')} 内容路径不存在,跳过: {info.content_path}")
                    continue
                torrents.append(info)
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

    # ---------------- v2:转移/辅种 ----------------

    def list_torrents(self, state: str = "all") -> List[TorrentInfo]:
        data = self._get("/api/v2/torrents/info")
        if not isinstance(data, list):
            return []
        torrents = [self._to_info(item) for item in data]
        if state == "seeding":
            return [t for t in torrents if t.seeding]
        if state == "completed":
            return [t for t in torrents if t.done]
        return torrents

    def get_torrent_file(self, hash: str) -> Optional[bytes]:
        """获取种子文件字节。

        优先 qB API torrents/export(跨主机可用);BT_backup 读盘兜底
        (缺失且 v4.4+ infohash_v1 不同则用 v1 重试)。
        """        # 主路径:API 导出(不依赖本地文件系统)
        if self._login():
            try:
                resp = self._client.get("/api/v2/torrents/export", params={"hash": hash})
                if resp.status_code == 200 and resp.content:
                    return resp.content
                logger.warning(f"[{self.name}] torrents/export 失败: {resp.status_code}")
            except httpx.HTTPError as e:
                logger.warning(f"[{self.name}] torrents/export 异常: {e}")
        # 兜底:读 BT_backup 磁盘文件
        if not self.conf.torrent_path:
            logger.warning(f"[{self.name}] 未配置 torrent_path,无法读盘兜底: {hash}")
            return None
        base = Path(self.conf.torrent_path)
        candidates = [hash]
        data = self._get("/api/v2/torrents/info", hashes=hash)
        if isinstance(data, list) and data:
            v1 = data[0].get("infohash_v1") or ""
            if v1 and v1.lower() != hash.lower():
                candidates.append(v1.lower())
        for h in candidates:
            path = base / f"{h}.torrent"
            try:
                return path.read_bytes()
            except OSError:
                continue
        logger.warning(f"[{self.name}] 种子文件不存在(BT_backup): {base} 候选: {candidates}")
        return None

    def get_torrent_files(self, hash: str) -> List[tuple]:
        """种子文件列表(torrents/files,跨主机可用)。"""
        data = self._get("/api/v2/torrents/files", hash=hash)
        if not isinstance(data, list):
            return []
        return [(f.get("name") or "", int(f.get("size") or 0)) for f in data]

    def add_torrent(self, data: Union[bytes, str], save_path: str, *,
                    paused: bool, category: str = "", tags: Optional[List[str]] = None,
                    skip_checking: bool = False) -> Tuple[bool, str]:
        if not self._login():
            return False, "登录失败"
        params = {
            "savepath": save_path,
            "paused": "true" if paused else "false",
            "autoTMM": "false",          # 必须关闭,否则 savepath 失效
            "root_folder": "false",      # 种子内路径原样
            "skip_checking": "true" if skip_checking else "false",
        }
        if category:
            params["category"] = category
        if tags:
            params["tags"] = ",".join(tags)
        try:
            if isinstance(data, bytes):
                files = {"torrents": (f"{time.time()}.torrent", data)}
                resp = self._client.post("/api/v2/torrents/add", data=params, files=files)
            else:
                params["urls"] = data
                resp = self._client.post("/api/v2/torrents/add", data=params)
            # 成功判定:旧版返回 200 "Ok.";qB 5.x 返回 200 JSON {"added_torrent_ids":[...]}
            if resp.status_code == 200:
                text = resp.text.strip()
                if "Ok" in text:
                    return True, text
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and (body.get("added_torrent_ids") or (body.get("success_count") or 0) > 0):
                    return True, text[:200]
            logger.error(f"[{self.name}] torrents/add 失败: {resp.status_code} {resp.text[:200]}")
            return False, resp.text[:200] or f"HTTP {resp.status_code}"
        except httpx.HTTPError as e:
            logger.error(f"[{self.name}] torrents/add 异常: {e}")
            return False, str(e)

    def recheck(self, hash: str) -> bool:
        return self._post("/api/v2/torrents/recheck", {"hashes": hash})

    def app_version(self) -> str:
        if not self._login():
            return ""
        try:
            resp = self._client.get("/api/v2/app/version")
            resp.raise_for_status()
            return resp.text.strip()
        except httpx.HTTPError as e:
            logger.error(f"[{self.name}] app/version 失败: {e}")
            return ""

    def has_torrent(self, hash: str) -> bool:
        data = self._get("/api/v2/torrents/info", hashes=hash)
        return isinstance(data, list) and bool(data)

    def get_tracker(self, hash: str) -> str:
        """按需取主 tracker announce(种子文件缺 announce 补丁用)。"""
        data = self._get("/api/v2/torrents/trackers", hash=hash)
        if not isinstance(data, list):
            return ""
        for t in data:
            url = t.get("url") or ""
            if url and url not in ("", "** [DHT] **", "** [PeX] **", "** [LSD] **"):
                return url
        return ""

"""HTTP API(标准库实现,零额外依赖)。

端点见设计文档 §4.8;除 /health 外均需 token(?token= 或 X-Token 头)。
"""
from __future__ import annotations

import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..engine import TransferEngine

logger = logging.getLogger("bt-media-organizer.api")

_ROUTE_RE = re.compile(r"^/api/v1/history/(\d+)/redo$")


def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    """请求处理器;server 引用挂在类属性上。"""

    server_version = "bt-media-organizer/0.1"
    engine: TransferEngine = None
    token: str = ""

    # ---------------- 基础 ----------------

    def log_message(self, fmt, *args):  # 静默默认访问日志(业务日志已足够)
        return

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return {}

    def _auth_ok(self) -> bool:
        query = parse_qs(urlparse(self.path).query)
        token = (query.get("token") or [None])[0]
        if not token:
            token = self.headers.get("X-Token")
        if not self.token:
            return True  # 未配置 token(配置校验已强制要求,此处防御)
        return bool(token) and hmac.compare_digest(token, self.token)

    def _denied(self):
        self._send(401, _json({"code": 401, "message": "unauthorized"}))

    # ---------------- 路由 ----------------

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, _json({"code": 0, "message": "ok", "data": {"status": "up"}}))
            return
        if not self._auth_ok():
            self._denied()
            return
        if path == "/api/v1/history":
            self._history()
        elif path == "/api/v1/queue":
            self._send(200, _json({"code": 0, "data": self.engine.status()}))
        else:
            self._send(404, _json({"code": 404, "message": "not found"}))

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if not self._auth_ok():
            self._denied()
            return
        body = self._read_body()
        if path == "/api/v1/transfer":
            self._transfer(body)
        elif path == "/api/v1/poll":
            self._poll(body)
        elif (m := _ROUTE_RE.match(path)):
            self._redo(int(m.group(1)))
        else:
            self._send(404, _json({"code": 404, "message": "not found"}))

    # ---------------- 业务 ----------------

    def _transfer(self, body: dict) -> None:
        source: Optional[Path] = None
        if body.get("path"):
            source = Path(body["path"])
        elif body.get("hash"):
            source = self._resolve_hash(body["hash"], body.get("downloader"))
            if source is None:
                self._send(404, _json({"code": 404, "message": "下载器任务不存在或内容路径不可用"}, 404)[0])
                return
        else:
            self._send(400, _json({"code": 400, "message": "缺少 path 或 hash"}, 400)[0])
            return

        result = self.engine.organize(
            source=source,
            download_hash=body.get("hash"),
            downloader=body.get("downloader"),
            preview=bool(body.get("preview")),
            force=bool(body.get("force")),
            transfer_type=body.get("transfer_type"),
            target_path=Path(body["target_path"]) if body.get("target_path") else None,
        )
        code = 0 if result.all_success else 1
        self._send(200, _json({"code": code, "data": _result_dict(result)}))

    def _poll(self, body: dict) -> None:
        data = self.engine.poll_once(downloader=body.get("downloader"))
        self._send(200, _json({"code": 0, "data": data}))

    def _redo(self, history_id: int) -> None:
        ok, message, result = self.engine.redo(history_id)
        self._send(200, _json({"code": 0 if ok else 1,
                               "message": message,
                               "data": _result_dict(result) if result else None}))

    def _history(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        status = (query.get("status") or [None])[0]
        try:
            limit = min(int((query.get("limit") or ["50"])[0]), 500)
            offset = int((query.get("offset") or ["0"])[0])
        except ValueError:
            limit, offset = 50, 0
        rows = self.engine.store.list(status=status, limit=limit, offset=offset)
        self._send(200, _json({"code": 0, "data": [
            {
                "id": r.id, "source": r.source_path, "target": r.target_path,
                "hash": r.download_hash, "downloader": r.downloader,
                "status": r.status, "message": r.message,
                "transfer_type": r.transfer_type, "created_at": r.created_at,
            } for r in rows
        ]}))

    def _resolve_hash(self, hash_: str, downloader: str = None) -> Optional[Path]:
        adapter = self.engine._resolve_adapter(downloader)
        if not adapter:
            return None
        for t in adapter.list_finished():
            if t.hash == hash_:
                return t.content_path if t.content_path.exists() else None
        return None


def _result_dict(result) -> dict:
    return {
        "total": result.total, "success": result.success, "failed": result.failed,
        "skipped": result.skipped, "all_success": result.all_success,
        "preview": result.preview, "message": result.message,
        "items": result.items,
    }


class ApiServer:
    """HTTP 服务封装。"""

    def __init__(self, conf: Config, engine: TransferEngine):
        _Handler.engine = engine
        _Handler.token = conf.server.token
        self._httpd = ThreadingHTTPServer((conf.server.host, conf.server.port), _Handler)
        logger.info(f"HTTP 服务已启动: http://{conf.server.host}:{conf.server.port}")

    def serve_forever(self):
        self._httpd.serve_forever()

    def shutdown(self):
        self._httpd.shutdown()
        self._httpd.server_close()

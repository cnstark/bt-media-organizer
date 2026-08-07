"""辅种记录存储(SQLite,WAL + 线程锁,沿用 HistoryStore 模式)。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reseed_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id TEXT NOT NULL,            -- 注入目标下载器
  source_hash TEXT NOT NULL,          -- 本地做种 hash
  site TEXT,                          -- Jackett 索引器 id
  torrent_id TEXT,                    -- Jackett 结果 id
  info_hash TEXT NOT NULL,            -- 目标种子 hash
  directory TEXT NOT NULL,            -- 原做种目录(注入 savepath)
  status TEXT NOT NULL,               -- pending/success/failed/skipped
  marker TEXT NOT NULL DEFAULT '',
  payload TEXT,                       -- JSON: {download_url, title}
  message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reseed_uniq
  ON reseed_records(client_id, info_hash);
CREATE INDEX IF NOT EXISTS idx_reseed_status ON reseed_records(status, created_at);
"""


@dataclass
class ReseedRecord:
    id: int
    client_id: str
    source_hash: str
    site: str
    torrent_id: str
    info_hash: str
    directory: str
    status: str
    marker: str
    payload: str
    message: Optional[str]
    created_at: str
    updated_at: str

    def payload_dict(self) -> dict:
        try:
            return json.loads(self.payload) if self.payload else {}
        except (json.JSONDecodeError, TypeError):
            return {}


class ReseedStore:
    """线程安全的辅种记录存储。"""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------------- 写 ----------------

    def add(self, client_id: str, source_hash: str, info_hash: str, directory: str,
            site: str = "", torrent_id: str = "", marker: str = "",
            payload: dict = None, status: str = "pending", message: str = None) -> Optional[int]:
        """插入记录。唯一键 (client_id, info_hash) 已存在 → 返回 None(幂等)。"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO reseed_records"
                "(client_id, source_hash, site, torrent_id, info_hash, directory,"
                " status, marker, payload, message, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (client_id, source_hash, site, torrent_id, info_hash, directory,
                 status, marker, json.dumps(payload, ensure_ascii=False) if payload else None,
                 message, now, now),
            )
            self._conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def update_status(self, record_id: int, status: str, message: str = None) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reseed_records SET status=?, message=?, updated_at=? WHERE id=?",
                (status, message, time.strftime("%Y-%m-%d %H:%M:%S"), record_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete(self, record_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM reseed_records WHERE id=?", (record_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------------- 读 ----------------

    def get(self, record_id: int) -> Optional[ReseedRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reseed_records WHERE id=?", (record_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def get_by_hash(self, client_id: str, info_hash: str) -> Optional[ReseedRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reseed_records WHERE client_id=? AND info_hash=?",
                (client_id, info_hash),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_by_source(self, client_id: str, source_hash: str) -> List[ReseedRecord]:
        """按来源种子 hash 列出全部记录(判断该种子是否已处理/可重试)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reseed_records WHERE client_id=? AND source_hash=?",
                (client_id, source_hash),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_by_sources(self, client_id: str, hashes: List[str]) -> List[ReseedRecord]:
        """按一组来源 hash(同一发布的跨站副本)批量查询记录。"""
        if not hashes:
            return []
        placeholders = ",".join("?" * len(hashes))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM reseed_records WHERE client_id=? AND source_hash IN ({placeholders})",
                [client_id] + list(hashes),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def exists_active(self, client_id: str, info_hash: str) -> bool:
        """pending/success/skipped 存在即视为已处理(failed 允许重试)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM reseed_records"
                " WHERE client_id=? AND info_hash=? AND status IN ('pending','success','skipped')",
                (client_id, info_hash),
            ).fetchone()
        return row is not None

    def list(self, status: str = None, limit: int = 50, offset: int = 0) -> List[ReseedRecord]:
        sql = "SELECT * FROM reseed_records"
        args: list = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_record(r) for r in rows]

    def counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS c FROM reseed_records GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["c"]) for r in rows}

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ReseedRecord:
        return ReseedRecord(
            id=row["id"], client_id=row["client_id"], source_hash=row["source_hash"],
            site=row["site"] or "", torrent_id=row["torrent_id"] or "",
            info_hash=row["info_hash"], directory=row["directory"],
            status=row["status"], marker=row["marker"] or "",
            payload=row["payload"], message=row["message"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

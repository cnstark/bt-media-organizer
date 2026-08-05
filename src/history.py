"""整理历史与 TMDB 缓存(SQLite,WAL 模式)。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_path TEXT NOT NULL,
  download_hash TEXT,
  downloader TEXT,
  target_path TEXT,
  meta_json TEXT,
  transfer_type TEXT,
  status TEXT NOT NULL,
  message TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_hash ON transfer_history(download_hash, status);
CREATE INDEX IF NOT EXISTS idx_history_src  ON transfer_history(source_path);
CREATE INDEX IF NOT EXISTS idx_history_status ON transfer_history(status, created_at);

CREATE TABLE IF NOT EXISTS media_cache (
  key TEXT PRIMARY KEY,
  json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


@dataclass
class HistoryRecord:
    id: int
    source_path: str
    download_hash: Optional[str]
    downloader: Optional[str]
    target_path: Optional[str]
    meta_json: Optional[str]
    transfer_type: Optional[str]
    status: str
    message: Optional[str]
    created_at: str


class HistoryStore:
    """线程安全的历史存储。"""

    def __init__(self, db_path: str, keep_days: int = 365):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.keep_days = keep_days

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------------- 历史 ----------------

    def add(self, source_path: str, status: str, download_hash: str = None,
            downloader: str = None, target_path: str = None, meta: dict = None,
            transfer_type: str = None, message: str = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO transfer_history"
                "(source_path, download_hash, downloader, target_path, meta_json,"
                " transfer_type, status, message, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (source_path, download_hash, downloader, target_path,
                 json.dumps(meta, ensure_ascii=False) if meta else None,
                 transfer_type, status, message,
                 time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_by_id(self, record_id: int) -> Optional[HistoryRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM transfer_history WHERE id=?", (record_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def get_success_by_source(self, source_path: str) -> Optional[HistoryRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM transfer_history WHERE source_path=? AND status='success'"
                " ORDER BY id DESC LIMIT 1",
                (source_path,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def success_count_by_hash(self, download_hash: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM transfer_history"
                " WHERE download_hash=? AND status='success'",
                (download_hash,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def fail_count_by_hash(self, download_hash: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM transfer_history"
                " WHERE download_hash=? AND status='failed'",
                (download_hash,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def delete(self, record_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM transfer_history WHERE id=?", (record_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def list(self, status: str = None, limit: int = 50, offset: int = 0) -> List[HistoryRecord]:
        sql = "SELECT * FROM transfer_history"
        args: list = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_record(r) for r in rows]

    def purge(self) -> int:
        """清理超过 keep_days 的历史(keep_days<=0 表示永久保留)。"""
        if self.keep_days <= 0:
            return 0
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - self.keep_days * 86400))
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM transfer_history WHERE created_at < ?", (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> HistoryRecord:
        return HistoryRecord(
            id=row["id"], source_path=row["source_path"],
            download_hash=row["download_hash"], downloader=row["downloader"],
            target_path=row["target_path"], meta_json=row["meta_json"],
            transfer_type=row["transfer_type"], status=row["status"],
            message=row["message"], created_at=row["created_at"],
        )

    # ---------------- TMDB 缓存 ----------------

    def cache_get(self, key: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT json FROM media_cache WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["json"])
        except (json.JSONDecodeError, TypeError):
            return None

    def cache_set(self, key: str, value: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO media_cache (key, json, created_at) VALUES (?,?,?)",
                (key, json.dumps(value, ensure_ascii=False),
                 time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            self._conn.commit()

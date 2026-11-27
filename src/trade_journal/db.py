"""本地 SQLite 数据层：K 线缓存、交易复盘日记、K 线锚点、交易纪律规则与截图附件。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CN = timezone(timedelta(hours=8))
DB_LOCK = threading.RLock()


class ThreadSafeConnection:
    """每线程独立 SQLite 连接（WAL 模式）+ 写操作可重入锁，彻底避免 FastAPI 多线程共享游标导致的 InterfaceError / 读脏。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._lock = DB_LOCK
        self._is_memory = db_path == ":memory:"
        self._shared: sqlite3.Connection | None = None
        # 立即在当前线程建连并初始化 PRAGMA
        self._conn()

    def _open_raw(self) -> sqlite3.Connection:
        raw = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA busy_timeout=15000")
        raw.execute("PRAGMA foreign_keys=ON")
        return raw

    def _conn(self) -> sqlite3.Connection:
        if self._is_memory:
            if self._shared is None:
                self._shared = self._open_raw()
            return self._shared
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._open_raw()
            self._local.conn = c
        return c

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if self._is_memory:
            with self._lock:
                return self._conn().execute(sql, parameters)
        return self._conn().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._conn().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        with self._lock:
            return self._conn().executescript(sql_script)

    def commit(self) -> None:
        with self._lock:
            self._conn().commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn().rollback()

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn(), name)


def data_dir() -> Path:
    raw = os.environ.get("TRADE_JOURNAL_DATA") or str(Path.home() / ".trade-journal")
    p = Path(raw).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def media_dir() -> Path:
    p = data_dir() / "media"
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_db_path() -> Path:
    return data_dir() / "journal.db"


def now_iso() -> str:
    return datetime.now(CN).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(CN).strftime("%Y-%m-%d")


def connect(path: Path | None = None) -> ThreadSafeConnection:
    db_file = path or default_db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    return ThreadSafeConnection(str(db_file))


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS candles (
            symbol      TEXT NOT NULL,
            interval    TEXT NOT NULL,
            ts          INTEGER NOT NULL,
            open        REAL NOT NULL,
            high        REAL NOT NULL,
            low         REAL NOT NULL,
            close       REAL NOT NULL,
            volume      REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (symbol, interval, ts)
        );

        CREATE TABLE IF NOT EXISTS cache_meta (
            symbol      TEXT NOT NULL,
            interval    TEXT NOT NULL,
            yahoo_sym   TEXT NOT NULL,
            currency    TEXT NOT NULL DEFAULT 'USD',
            exchange    TEXT NOT NULL DEFAULT '',
            bar_count   INTEGER NOT NULL DEFAULT 0,
            fetched_at  TEXT NOT NULL,
            PRIMARY KEY (symbol, interval)
        );

        CREATE TABLE IF NOT EXISTS entries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date      TEXT NOT NULL,
            title           TEXT NOT NULL,
            symbol          TEXT NOT NULL DEFAULT 'SPX',
            interval        TEXT NOT NULL DEFAULT '15m',
            outcome         TEXT NOT NULL DEFAULT 'review',
            pnl             TEXT NOT NULL DEFAULT '',
            bias_tags_json  TEXT NOT NULL DEFAULT '[]',
            raw_note        TEXT NOT NULL DEFAULT '',
            context_note    TEXT NOT NULL DEFAULT '',
            symbols_json    TEXT NOT NULL DEFAULT '[]',
            execution_note  TEXT NOT NULL DEFAULT '',
            did_right_note  TEXT NOT NULL DEFAULT '',
            should_have_note TEXT NOT NULL DEFAULT '',
            takeaway_note   TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS markers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id    INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            kind        TEXT NOT NULL,
            ts          INTEGER NOT NULL,
            price       REAL NOT NULL,
            label       TEXT NOT NULL DEFAULT '',
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id    INTEGER REFERENCES entries(id) ON DELETE SET NULL,
            symbol      TEXT NOT NULL DEFAULT '',
            rule_text   TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT '执行纪律',
            hit_count   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id    INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            filename    TEXT NOT NULL,
            caption     TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS coach_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_day TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            regime      TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_plans (
            plan_date            TEXT PRIMARY KEY,
            stance               TEXT NOT NULL DEFAULT 'do_nothing',
            plan_note            TEXT NOT NULL DEFAULT '',
            step1_note           TEXT NOT NULL DEFAULT '',
            step2_note           TEXT NOT NULL DEFAULT '',
            step3_note           TEXT NOT NULL DEFAULT '',
            market_snapshot_json TEXT NOT NULL DEFAULT '{}',
            focus_symbols_json   TEXT NOT NULL DEFAULT '[]',
            plan_followed        TEXT NOT NULL DEFAULT '',
            evening_review       TEXT NOT NULL DEFAULT '',
            signed_at            TEXT NOT NULL DEFAULT '',
            reviewed_at          TEXT NOT NULL DEFAULT '',
            updated_at           TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS market_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date  TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT 'custom',
            title       TEXT NOT NULL,
            impact      TEXT NOT NULL DEFAULT 'high',
            time_label  TEXT NOT NULL DEFAULT '',
            symbol      TEXT NOT NULL DEFAULT '',
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );
        """
    )
    # 兼容老库无损迁移新增列
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)").fetchall()}
    for col, sql_def in (
        ("raw_note", "TEXT NOT NULL DEFAULT ''"),
        ("context_note", "TEXT NOT NULL DEFAULT ''"),
        ("symbols_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("did_right_note", "TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE entries ADD COLUMN {col} {sql_def}")

    plan_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_plans)").fetchall()}
    for col, sql_def in (
        ("step1_note", "TEXT NOT NULL DEFAULT ''"),
        ("step2_note", "TEXT NOT NULL DEFAULT ''"),
        ("step3_note", "TEXT NOT NULL DEFAULT ''"),
        ("market_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if col not in plan_cols:
            conn.execute(f"ALTER TABLE daily_plans ADD COLUMN {col} {sql_def}")
    conn.commit()


def entry_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    eid = row["id"]
    markers = [
        dict(r)
        for r in conn.execute(
            "SELECT id, entry_id, kind, ts, price, label, note, created_at FROM markers WHERE entry_id=? ORDER BY ts ASC, id ASC",
            (eid,),
        ).fetchall()
    ]
    attachments = [
        {
            "id": r["id"],
            "entry_id": r["entry_id"],
            "filename": r["filename"],
            "caption": r["caption"],
            "url": f"/api/media/{r['filename']}",
            "created_at": r["created_at"],
        }
        for r in conn.execute(
            "SELECT id, entry_id, filename, caption, created_at FROM attachments WHERE entry_id=? ORDER BY id DESC",
            (eid,),
        ).fetchall()
    ]
    try:
        bias_tags = json.loads(row["bias_tags_json"] or "[]")
    except ValueError:
        bias_tags = []
    try:
        symbols = json.loads(row["symbols_json"] or "[]")
    except ValueError:
        symbols = []
    return {
        "id": eid,
        "trade_date": row["trade_date"],
        "title": row["title"],
        "symbol": row["symbol"],
        "interval": row["interval"],
        "outcome": row["outcome"],
        "pnl": row["pnl"],
        "bias_tags": bias_tags,
        "raw_note": row["raw_note"] or "",
        "context_note": row["context_note"] or "",
        "symbols": symbols,
        "execution_note": row["execution_note"],
        "did_right_note": row["did_right_note"] or "",
        "should_have_note": row["should_have_note"],
        "takeaway_note": row["takeaway_note"],
        "markers": markers,
        "attachments": attachments,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def plan_to_dict(row: sqlite3.Row | None, plan_date: str = "") -> dict[str, Any]:
    if row is None:
        return {
            "plan_date": plan_date or today_str(),
            "stance": "do_nothing",
            "plan_note": "",
            "step1_note": "",
            "step2_note": "",
            "step3_note": "",
            "market_snapshot": {},
            "focus_symbols": [],
            "plan_followed": "",
            "evening_review": "",
            "signed_at": "",
            "reviewed_at": "",
            "updated_at": "",
            "is_signed": False,
            "is_reviewed": False,
            "has_plan": False,
        }
    try:
        focus = json.loads(row["focus_symbols_json"] or "[]")
    except ValueError:
        focus = []
    try:
        msnap = json.loads(row["market_snapshot_json"] or "{}")
    except ValueError:
        msnap = {}
    s1 = row["step1_note"] or ""
    s2 = row["step2_note"] or ""
    s3 = row["step3_note"] or ""
    pnote = row["plan_note"] or ""
    has_plan = bool(s1.strip() or s2.strip() or s3.strip() or pnote.strip() or row["signed_at"])
    return {
        "plan_date": row["plan_date"],
        "stance": row["stance"],
        "plan_note": pnote,
        "step1_note": s1,
        "step2_note": s2,
        "step3_note": s3,
        "market_snapshot": msnap,
        "focus_symbols": focus,
        "plan_followed": row["plan_followed"],
        "evening_review": row["evening_review"],
        "signed_at": row["signed_at"],
        "reviewed_at": row["reviewed_at"],
        "updated_at": row["updated_at"],
        "is_signed": bool(row["signed_at"]),
        "is_reviewed": bool(row["reviewed_at"]),
        "has_plan": has_plan,
    }


def daily_plan_streak(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT plan_date, signed_at, reviewed_at FROM daily_plans WHERE signed_at != '' ORDER BY plan_date DESC"
    ).fetchall()
    signed_dates = [r["plan_date"] for r in rows]
    reviewed_total = sum(1 for r in rows if r["reviewed_at"])
    streak = 0
    if signed_dates:
        cur = datetime.strptime(today_str(), "%Y-%m-%d").date()
        date_set = set(signed_dates)
        # 如果今天还没签，从昨天开始算当前连续天数
        if cur.isoformat() not in date_set:
            cur = cur - timedelta(days=1)
        while cur.isoformat() in date_set:
            streak += 1
            cur = cur - timedelta(days=1)
    return {"streak": streak, "totalSigned": len(signed_dates), "totalReviewed": reviewed_total}

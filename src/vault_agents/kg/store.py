"""Temporal Knowledge Graph: entities + triples с интервалами valid_from/valid_to.

Схема — конвергентная из нескольких live-проектов (ticket-041):
факты не перезаписываются, а закрываются по времени (valid_to), поэтому
история отвечает на вопрос «что считалось истинным тогда-то».
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS triples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject);
CREATE INDEX IF NOT EXISTS idx_triples_valid ON triples(valid_from, valid_to);
"""


@dataclass
class Entity:
    name: str
    kind: str


@dataclass
class Fact:
    subject: str
    predicate: str
    object: str
    valid_from: str
    valid_to: str | None
    source: str
    id: int | None = None


class KG:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # MCP-сервер вызывает тулсы в worker-потоках: соединение обязано
        # переживать смену потока, а запись — сериализоваться локом.
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._mutex = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- entities ----------

    def add_entity(self, name: str, kind: str = "") -> Entity:
        with self._mutex:
            return self._add_entity_locked(name, kind)

    def _add_entity_locked(self, name: str, kind: str) -> Entity:
        self.conn.execute(
            "INSERT INTO entities(name, kind) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind WHERE excluded.kind != ''",
            (name.strip(), kind.strip()),
        )
        self.conn.commit()
        return Entity(name=name.strip(), kind=kind.strip())

    def entities(self, limit: int = 100) -> list[Entity]:
        rows = self.conn.execute(
            "SELECT name, kind FROM entities ORDER BY name LIMIT ?", (limit,)
        ).fetchall()
        return [Entity(name=r["name"], kind=r["kind"]) for r in rows]

    # ---------- facts ----------

    def add_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        source: str = "",
    ) -> Fact:
        with self._mutex:
            return self._add_fact_locked(subject, predicate, obj, valid_from, valid_to, source)

    def _add_fact_locked(
        self,
        subject: str,
        predicate: str,
        obj: str,
        valid_from: str | None,
        valid_to: str | None,
        source: str,
    ) -> Fact:
        """Новый факт закрывает предыдущий открытый с той же парой
        (subject, predicate) вчерашним днём — если valid_to не задан явно."""
        vf = (valid_from or date.today().isoformat()).strip()
        subj, pred, obj = subject.strip(), predicate.strip(), obj.strip()
        prev = self.conn.execute(
            "SELECT id FROM triples WHERE subject=? AND predicate=? AND valid_to IS NULL",
            (subj, pred),
        ).fetchall()
        for row in prev:
            close = valid_to or _day_before(vf)
            self.conn.execute("UPDATE triples SET valid_to=? WHERE id=?", (close, row["id"]))
        cur = self.conn.execute(
            "INSERT INTO triples(subject, predicate, object, valid_from, valid_to, source) "
            "VALUES(?,?,?,?,?,?)",
            (subj, pred, obj, vf, valid_to, source),
        )
        self.conn.commit()
        return Fact(
            subject=subj, predicate=pred, object=obj,
            valid_from=vf, valid_to=valid_to, source=source, id=cur.lastrowid,
        )

    def query(
        self,
        subject: str = "",
        predicate: str = "",
        at: str | None = None,
    ) -> list[Fact]:
        """Факты; `at` — «что было истинно на дату»."""
        sql = "SELECT * FROM triples WHERE 1=1"
        args: list[str] = []
        if subject:
            sql += " AND subject=?"
            args.append(subject)
        if predicate:
            sql += " AND predicate=?"
            args.append(predicate)
        if at:
            sql += " AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)"
            args += [at, at]
        sql += " ORDER BY subject, predicate, valid_from"
        return [_row_to_fact(r) for r in self.conn.execute(sql, args).fetchall()]

    def timeline(self, subject: str) -> list[Fact]:
        rows = self.conn.execute(
            "SELECT * FROM triples WHERE subject=? ORDER BY valid_from", (subject,)
        ).fetchall()
        return [_row_to_fact(r) for r in rows]

    def stats(self) -> dict:
        ents = self.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        trips = self.conn.execute("SELECT COUNT(*) c FROM triples").fetchone()["c"]
        open_ = self.conn.execute(
            "SELECT COUNT(*) c FROM triples WHERE valid_to IS NULL"
        ).fetchone()["c"]
        return {"entities": ents, "triples": trips, "open": open_, "db": str(self.path)}

    def close(self) -> None:
        self.conn.close()


def _day_before(iso: str) -> str:
    try:
        y, m, d = (int(x) for x in iso.split("-"))
        return date(y, m, d).fromordinal(date(y, m, d).toordinal() - 1).isoformat()
    except (ValueError, TypeError):
        return iso


def _row_to_fact(r: sqlite3.Row) -> Fact:
    return Fact(
        subject=r["subject"],
        predicate=r["predicate"],
        object=r["object"],
        valid_from=r["valid_from"],
        valid_to=r["valid_to"],
        source=r["source"],
        id=r["id"],
    )

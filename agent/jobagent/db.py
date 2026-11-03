"""SQLite-хранилище: вакансии (они же очередь), события воронки, доставки вебхуков.

Статусы вакансии:
  queued   — ждёт обработки воркером;
  baseline — была в фильтре до первого запуска, не оценивается;
  rejected — отсеяна (stage: already / rules / triage / stale / company / prefilter / score / foreign);
  shown    — карточка отправлена в бот;
  applied  — Александр откликнулся;
  skipped  — Александр пропустил (reason — причина);
  error    — обработка упала несколько раз подряд.

Поле stage: для rejected — ступень отсева, для applied — этап у работодателя
(interview / offer / rejected_by_employer).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    slug          TEXT PRIMARY KEY,
    vacancy_id    INTEGER UNIQUE,
    title         TEXT NOT NULL,
    company       TEXT,
    feeds         TEXT NOT NULL DEFAULT '[]',
    card          TEXT NOT NULL,
    detail        TEXT,
    assessment    TEXT,
    score         INTEGER,
    status        TEXT NOT NULL,
    stage         TEXT,
    reason        TEXT,
    source        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    tg_message_id INTEGER,
    tracker_id    INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vacancies_status ON vacancies(status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT,
    type       TEXT NOT NULL,
    data       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_type_time ON events(type, created_at);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event       TEXT,
    received_at TEXT NOT NULL
);

-- версии пакетов отклика: status building / ready / failed, path — папка с файлами
CREATE TABLE IF NOT EXISTS packages (
    slug          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL,
    path          TEXT,
    tg_message_id INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (slug, version)
);
"""

PACKAGE_FIELDS = {"status", "path", "tg_message_id"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def company_key(name: str) -> str:
    return name.strip().casefold()


def vacancy_id_from_slug(slug: str) -> int | None:
    m = re.match(r"(\d+)-", slug)
    return int(m.group(1)) if m else None


@dataclass
class Vacancy:
    slug: str
    vacancy_id: int | None
    title: str
    company: str | None
    feeds: list[dict[str, Any]]
    card: dict[str, Any]
    detail: dict[str, Any] | None
    assessment: dict[str, Any] | None
    score: int | None
    status: str
    stage: str | None
    reason: str | None
    source: str
    attempts: int
    tg_message_id: int | None
    tracker_id: int | None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Vacancy:
        def js(v: str | None) -> Any:
            return json.loads(v) if v else None

        return cls(
            slug=row["slug"],
            vacancy_id=row["vacancy_id"],
            title=row["title"],
            company=row["company"],
            feeds=js(row["feeds"]) or [],
            card=js(row["card"]) or {},
            detail=js(row["detail"]),
            assessment=js(row["assessment"]),
            score=row["score"],
            status=row["status"],
            stage=row["stage"],
            reason=row["reason"],
            source=row["source"],
            attempts=row["attempts"],
            tg_message_id=row["tg_message_id"],
            tracker_id=row["tracker_id"],
        )


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() не вызван")
        return self._conn

    # --- события ---

    async def log_event(self, type_: str, slug: str | None = None, **data: Any) -> None:
        await self.conn.execute(
            "INSERT INTO events (slug, type, data, created_at) VALUES (?, ?, ?, ?)",
            (slug, type_, json.dumps(data, ensure_ascii=False) if data else None, now_iso()),
        )
        await self.conn.commit()

    async def has_event(self, type_: str) -> bool:
        async with self.conn.execute("SELECT 1 FROM events WHERE type = ? LIMIT 1", (type_,)) as cur:
            return await cur.fetchone() is not None

    # --- доставки вебхуков ---

    async def remember_delivery(self, delivery_id: str, event: str) -> bool:
        """True, если доставка новая; False, если это повтор."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO deliveries (delivery_id, event, received_at) VALUES (?, ?, ?)",
            (delivery_id, event, now_iso()),
        )
        await self.conn.commit()
        return cur.rowcount == 1

    # --- вакансии ---

    async def count_vacancies(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM vacancies") as cur:
            (n,) = await cur.fetchone()
        return n

    async def upsert_card(
        self, card: dict[str, Any], feeds: list[dict[str, Any]], source: str, status: str = "queued"
    ) -> bool:
        """Добавляет вакансию по карточке. Возвращает True, если вакансия новая.

        Для уже известной вакансии только дописывает фильтры, в которых она нашлась.
        """
        slug = card["slug"]
        async with self.conn.execute("SELECT feeds FROM vacancies WHERE slug = ?", (slug,)) as cur:
            row = await cur.fetchone()
        if row is not None:
            known = json.loads(row["feeds"])
            ids = {f["id"] for f in known}
            merged = known + [f for f in feeds if f["id"] not in ids]
            if len(merged) != len(known):
                await self.conn.execute(
                    "UPDATE vacancies SET feeds = ?, updated_at = ? WHERE slug = ?",
                    (json.dumps(merged, ensure_ascii=False), now_iso(), slug),
                )
                await self.conn.commit()
            return False

        ts = now_iso()
        await self.conn.execute(
            """INSERT INTO vacancies
               (slug, vacancy_id, title, card, feeds, status, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                slug,
                card.get("vacancy_id") or vacancy_id_from_slug(slug),
                card.get("title") or slug,
                json.dumps(card, ensure_ascii=False),
                json.dumps(feeds, ensure_ascii=False),
                status,
                source,
                ts,
                ts,
            ),
        )
        await self.conn.commit()
        if status == "queued":
            await self.log_event("received", slug, source=source, feeds=[f["id"] for f in feeds])
        return True

    async def get(self, slug: str) -> Vacancy | None:
        async with self.conn.execute("SELECT * FROM vacancies WHERE slug = ?", (slug,)) as cur:
            row = await cur.fetchone()
        return Vacancy.from_row(row) if row else None

    async def get_by_id(self, vacancy_id: int) -> Vacancy | None:
        async with self.conn.execute(
            "SELECT * FROM vacancies WHERE vacancy_id = ?", (vacancy_id,)
        ) as cur:
            row = await cur.fetchone()
        return Vacancy.from_row(row) if row else None

    async def next_queued(self) -> Vacancy | None:
        async with self.conn.execute(
            "SELECT * FROM vacancies WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return Vacancy.from_row(row) if row else None

    async def skipped_companies(self) -> set[str]:
        """Компании, пропущенные с причиной «Компания неинтересна», через casefold.
        Сравниваем в Python: lower() в SQLite не трогает кириллицу."""
        async with self.conn.execute(
            "SELECT DISTINCT company FROM vacancies"
            " WHERE status = 'skipped' AND reason = 'company' AND company IS NOT NULL"
        ) as cur:
            return {company_key(row[0]) async for row in cur}

    async def shown_of_company(self, company: str) -> list[Vacancy]:
        """Карточки этой компании, по которым ещё нет решения."""
        async with self.conn.execute(
            "SELECT * FROM vacancies WHERE status = 'shown' AND company IS NOT NULL ORDER BY created_at"
        ) as cur:
            rows = [Vacancy.from_row(row) async for row in cur]
        return [v for v in rows if company_key(v.company or "") == company_key(company)]

    async def update(self, slug: str, **fields: Any) -> None:
        if not fields:
            return
        for key in ("detail", "assessment"):
            if key in fields and fields[key] is not None:
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(
            f"UPDATE vacancies SET {cols} WHERE slug = ?", (*fields.values(), slug)
        )
        await self.conn.commit()

    # --- пакеты отклика ---

    async def create_package(self, slug: str) -> int:
        """Новая версия пакета (следующий номер по вакансии) со статусом building."""
        ts = now_iso()
        async with self.conn.execute(
            """INSERT INTO packages (slug, version, status, created_at, updated_at)
               SELECT ?, COALESCE(MAX(version), 0) + 1, 'building', ?, ? FROM packages WHERE slug = ?
               RETURNING version""",
            (slug, ts, ts, slug),
        ) as cur:
            (version,) = await cur.fetchone()
        await self.conn.commit()
        return version

    async def set_package(self, slug: str, version: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - PACKAGE_FIELDS
        if unknown:
            raise ValueError(f"нет полей пакета: {sorted(unknown)}")
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(
            f"UPDATE packages SET {cols} WHERE slug = ? AND version = ?", (*fields.values(), slug, version)
        )
        await self.conn.commit()

    async def get_package(self, slug: str, version: int) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM packages WHERE slug = ? AND version = ?", (slug, version)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def building_package(self, slug: str) -> int | None:
        async with self.conn.execute(
            "SELECT MAX(version) FROM packages WHERE slug = ? AND status = 'building'", (slug,)
        ) as cur:
            (version,) = await cur.fetchone()
        return version

    async def fail_stale_packages(self) -> int:
        """После перезапуска сервиса сборка, которая шла, уже не идёт: building → failed (code restart)."""
        async with self.conn.execute("SELECT slug, version FROM packages WHERE status = 'building'") as cur:
            stale = [(r["slug"], r["version"]) for r in await cur.fetchall()]
        for slug, version in stale:
            await self.set_package(slug, version, status="failed")
            await self.log_event("package_failed", slug, version=version, code="restart")
        return len(stale)

    # --- статистика ---

    async def event_counts(self, days: int | None) -> dict[str, int]:
        """Счётчики событий за последние `days` дней (None — за всё время).

        Ключи: received, shown, applied, skipped, interview, offer, rejected_by_employer,
        rejected:<stage>.
        """
        sql = "SELECT type, data FROM events"
        args: tuple[Any, ...] = ()
        if days is not None:
            since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
            sql += " WHERE created_at >= ?"
            args = (since,)
        counts: dict[str, int] = {}
        async with self.conn.execute(sql, args) as cur:
            async for row in cur:
                key = row["type"]
                if key == "rejected" and row["data"]:
                    key = f"rejected:{json.loads(row['data']).get('stage', '?')}"
                counts[key] = counts.get(key, 0) + 1
        return counts

    async def llm_usage(self, days: int | None, step_prefix: str = "") -> list[dict[str, Any]]:
        """Токены вызовов моделей (события llm) за `days` дней, шаги которых начинаются со step_prefix."""
        sql = "SELECT data FROM events WHERE type = 'llm'"
        args: tuple[Any, ...] = ()
        if days is not None:
            since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
            sql += " AND created_at >= ?"
            args = (since,)
        out = []
        async with self.conn.execute(sql + " ORDER BY id", args) as cur:
            async for row in cur:
                data = json.loads(row["data"] or "{}")
                if str(data.pop("step", "")).startswith(step_prefix):
                    out.append(data)
        return out

    async def skip_reasons(self, days: int | None) -> dict[str, int]:
        sql = "SELECT data FROM events WHERE type = 'skipped'"
        args: tuple[Any, ...] = ()
        if days is not None:
            since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
            sql += " AND created_at >= ?"
            args = (since,)
        out: dict[str, int] = {}
        async with self.conn.execute(sql, args) as cur:
            async for row in cur:
                reason = json.loads(row["data"] or "{}").get("reason", "?")
                out[reason] = out.get(reason, 0) + 1
        return out

"""Текст карточки вакансии и статистики для Telegram (HTML-разметка)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from typing import Any

from .pricing import usage_cost

RU_LABELS = {
    "confirmed": "✅",
    "indirect": "🟡",
    "hidden": "❓",
    "foreign": "❌",
}

FORMAT_LABELS = {
    "remote_from_russia": "удалёнка из России",
    "remote_abroad": "удалёнка, нужно жить в другой стране",
    "office_relocation": "офис с релокацией",
    "unsuitable": "формат не подходит",
    "unknown": "формат неясен",
}

SKIP_REASONS = {
    "role": "Роль или грейд",
    "terms": "Условия или ЗП",
    "ru": "Не RU-компания",
    "company": "Компания неинтересна",
    "other": "Другое",
}

STAGES = {
    "interview": ("HR Interview", "🎤 Позвали на интервью"),
    "offer": ("Offer", "🎉 Оффер"),
    "rejected_by_employer": ("Rejected", "✖️ Отказ работодателя"),
}


def vacancy_url(slug: str, detail: dict[str, Any] | None = None) -> str:
    if detail and detail.get("url"):
        return str(detail["url"]).split("?")[0]
    return f"https://hirify.me/jobs/{slug}"


DETAILS_HEADER = "📋 Подробно"
MAX_PEOPLE = 6
TELEGRAM_LIMIT = 4096


def posted_ago(created_at: str | None, now: datetime | None = None) -> str:
    if not created_at:
        return ""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    days = ((now or datetime.now(UTC)).date() - created.astimezone(UTC).date()).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{days} дн. назад"


def _people_line(p: dict[str, Any]) -> str:
    """Имя и должность из заголовка Google, плюс начало сниппета (там город и вуз)."""
    name = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", p.get("title") or "").strip()
    snippet = (p.get("snippet") or "").strip()
    if len(snippet) > 90:
        snippet = snippet[:90].rsplit(" ", 1)[0] + "…"
    head = f'<a href="{escape(p["url"], quote=True)}">{escape(name)}</a>' if p.get("url") else escape(name)
    return f"• {head}" + (f" — {escape(snippet)}" if snippet else "")


def _contact_item(c: dict[str, Any]) -> str:
    value = str(c.get("value") or "").strip()
    kind = c.get("type")
    if kind == "url" or value.startswith(("http://", "https://")):
        return f'<a href="{escape(value, quote=True)}">{escape(value.split("://", 1)[-1])}</a>'
    if kind == "telegram" or value.startswith("@"):
        handle = value.lstrip("@").removeprefix("https://t.me/")
        return f'<a href="https://t.me/{escape(handle, quote=True)}">@{escape(handle)}</a>'
    return escape(value)


def contact_url(detail: dict[str, Any] | None) -> str | None:
    """Первая ссылка из раскрытого контакта: её ставим на кнопку."""
    for c in ((detail or {}).get("contact") or {}).get("contacts") or []:
        value = str(c.get("value") or "")
        if value.startswith(("http://", "https://")):
            return value
    return None


def contact_line(detail: dict[str, Any] | None) -> str:
    """Раскрытый по кнопке «📇 Контакт» способ отклика; пусто, пока не раскрывали."""
    contact = (detail or {}).get("contact")
    if contact is None:
        return ""
    items = [_contact_item(c) for c in contact.get("contacts") or [] if c.get("value")]
    if contact.get("linkedin"):
        items.append(f'<a href="{escape(contact["linkedin"], quote=True)}">LinkedIn</a>')
    return "📇 Контакт: " + (", ".join(items) if items else "Hirify не вернул контакт")


def render_card(
    title: str, a: dict[str, Any], feeds: list[dict[str, Any]], detail: dict[str, Any] | None
) -> str:
    """Короткая карточка: решение «откликаться или нет» за несколько секунд."""
    d = detail or {}
    company = a.get("company") or d.get("company") or "компания скрыта"
    ru = RU_LABELS.get(a.get("ru_label", ""), "❓")
    # ru_summary появился 2026-10-27; в старых оценках есть только ru_evidence.
    ru_text = a.get("ru_summary") or a.get("ru_evidence") or ""
    lines = [
        f"<b>{a['score']}/5 · {escape(a.get('role') or title)}</b>",
        f"{escape(company)} · RU {ru} {escape(ru_text)}".rstrip(),
    ]
    if a.get("ru_linkedin"):
        lines.append(f"🔎 LinkedIn: {escape(a['ru_linkedin'])}")
    terms = [
        "🏠 " + (FORMAT_LABELS.get(a.get("format_priority", "")) or a.get("location_format") or "формат неясен"),
        "💰 " + (a.get("salary") or "не указана"),
    ]
    if a.get("grade"):
        terms.append(a["grade"])
    if ago := posted_ago(d.get("created_at")):
        terms.append(f"🕒 {ago}")
    lines.append(escape(" · ".join(terms)))
    lines.append("")
    lines.append(f"<b>{escape(a.get('verdict') or '')}</b>")
    lines.append("")
    for pro in (a.get("pros") or [])[:2]:
        lines.append(f"➕ {escape(pro)}")
    if a.get("con"):
        lines.append(f"➖ {escape(a['con'])}")
    if a.get("questions"):
        lines.append("❓ Уточнить: " + escape(", ".join(a["questions"][:3])))
    elif a.get("what_would_change"):
        lines.append(f"❓ {escape(a['what_would_change'])}")
    if contact := contact_line(d):
        lines += ["", contact]
    return "\n".join(lines)


def render_details(a: dict[str, Any], feeds: list[dict[str, Any]], detail: dict[str, Any] | None) -> str:
    """Раскрывается кнопкой «Подробнее» под короткой карточкой."""
    d = detail or {}
    lines = [f"\n━━━━━━━━━━\n<b>{DETAILS_HEADER}</b>"]
    if a.get("location_format"):
        lines.append(f"📍 {escape(a['location_format'])}")

    missing = list(a.get("missing_must_haves") or [])
    must = list(a.get("must_haves") or [])
    if must or missing:
        lines += ["", "<b>Обязательные требования</b>"]
        lines += [f"{'✗' if m in missing else '✓'} {escape(m)}" for m in must]
        lines += [f"✗ {escape(m)}" for m in missing if m not in must]

    lines += ["", f"<b>RU {RU_LABELS.get(a.get('ru_label', ''), '❓')}</b>"]
    if a.get("ru_evidence"):
        lines.append(escape(a["ru_evidence"]))
    if a.get("ru_linkedin"):
        lines.append(f"🔎 LinkedIn: {escape(a['ru_linkedin'])}")
    research = d.get("linkedin_research") or {}
    people = research.get("people") or []
    if people:
        lines.append(f"Сотрудники в LinkedIn (найдено {len(people)}):")
        lines += [_people_line(p) for p in people[:MAX_PEOPLE]]
    company = research.get("company") or {}
    facts = [company.get("headquarters"), company.get("size")]
    if any(facts):
        name = escape(company.get("name") or "Компания")
        if company.get("url"):
            name = f'<a href="{escape(company["url"], quote=True)}">{name}</a>'
        lines.append(f"{name}: " + escape(", ".join(f for f in facts if f)))

    if a.get("red_flags"):
        lines += ["", "<b>Флаги</b>"] + [f"⚠️ {escape(f)}" for f in a["red_flags"]]

    channel = "кнопка на Hirify" if d.get("can_apply_directly") else "контакт или форма в вакансии"
    feed_names = ", ".join(f.get("name") or str(f.get("id")) for f in feeds)
    lines += ["", f"<i>Отклик: {channel}. Лента: {escape(feed_names)}</i>"]
    return "\n".join(lines)


def render_full(
    title: str, a: dict[str, Any], feeds: list[dict[str, Any]], detail: dict[str, Any] | None, expanded: bool
) -> str:
    text = render_card(title, a, feeds, detail)
    if not expanded:
        return text
    full = text + "\n" + render_details(a, feeds, detail)
    if len(full) > TELEGRAM_LIMIT:
        # Обрезаем по строкам, чтобы не разорвать HTML-тег.
        kept = []
        for line in full.split("\n"):
            if len("\n".join(kept + [line])) > TELEGRAM_LIMIT - 2:
                break
            kept.append(line)
        full = "\n".join(kept + ["…"])
    return full


def _pct(part: int, whole: int) -> str:
    return f" ({part * 100 // whole}%)" if whole else ""


TOKEN_LABELS = {
    "input_tokens": "ввод",
    "output_tokens": "вывод",
    "cache_read_tokens": "кеш чтение",
    "cache_write_tokens": "кеш запись",
}


def render_packages(counts: dict[str, int], package_usage: list[dict[str, Any]]) -> str | None:
    """Строка о пакетах отклика: собрано, переделок, средняя стоимость собранного пакета.

    В среднюю входят и затраты сборок, которые упали: это цена одного пакета на руках.
    """
    ready = counts.get("package_ready", 0)
    redo = counts.get("package_redo", 0)
    started = ready + redo + counts.get("package_requested", 0) + counts.get("package_failed", 0)
    if not started and not package_usage:
        return None
    costs = [usage_cost(u) for u in package_usage]
    total = sum(c for c in costs if c is not None)
    unpriced = [u for u, c in zip(package_usage, costs) if c is None]
    line = f"Пакеты: {ready} собрано, {redo} переделок"
    line += f", средняя стоимость ${total / ready:.2f}" if ready else f", потрачено ${total:.2f}"
    if unpriced:
        # Модель задана переменной окружения, а в PRICES её нет: её затраты видны токенами.
        models = ", ".join(escape(m) for m in sorted({u.get("model") or "?" for u in unpriced}))
        tokens = {k: sum(u.get(k) or 0 for u in unpriced) for k in TOKEN_LABELS}
        line += f" без {models}; токены без цены: " + ", ".join(
            f"{label} {tokens[k]}" for k, label in TOKEN_LABELS.items()
        )
    return line


def render_stats(
    counts: dict[str, int],
    skip_reasons: dict[str, int],
    period: str,
    package_usage: list[dict[str, Any]] | None = None,
) -> str:
    received = counts.get("received", 0)
    rejected = {k.split(":", 1)[1]: v for k, v in counts.items() if k.startswith("rejected:")}
    shown = counts.get("shown", 0)
    applied = counts.get("applied", 0)
    interview = counts.get("interview", 0)
    stage_names = {
        "already": "уже откликался",
        "rules": "правила",
        "triage": "быстрая модель по карточке",
        "stale": "старые",
        "prefilter": "быстрая модель по тексту",
        "score": "оценка ниже порога",
    }
    lines = [f"<b>Воронка {escape(period)}</b>", f"Получено: {received}"]
    total_rejected = sum(rejected.values())
    lines.append(f"Отсеяно: {total_rejected}{_pct(total_rejected, received)}")
    for stage, name in stage_names.items():
        if rejected.get(stage):
            lines.append(f"  · {name}: {rejected[stage]}")
    lines += [
        f"Показано: {shown}{_pct(shown, received)}",
        f"Откликнулся: {applied}{_pct(applied, shown)}",
        f"Пропустил: {counts.get('skipped', 0)}",
        f"Интервью: {interview}{_pct(interview, applied)}",
        f"Офферы: {counts.get('offer', 0)}",
        f"Отказы работодателей: {counts.get('rejected_by_employer', 0)}",
    ]
    if counts.get("error"):
        lines.append(f"Ошибки обработки: {counts['error']}")
    if packages := render_packages(counts, package_usage or []):
        lines.append(packages)
    if skip_reasons:
        lines.append("")
        lines.append("<b>Почему пропускал</b>")
        for code, n in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"  · {escape(SKIP_REASONS.get(code, code))}: {n}")
    return "\n".join(lines)

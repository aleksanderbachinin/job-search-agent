"""PDF резюме: одна страница A4 из ResumeDraft.

Стили не сжимаются никогда: если резюме не помещается, режется содержание по CUT_ORDER.
В ручном процессе скрипты «ужимали» межстрочный интервал под страницу, и строки наезжали друг на друга.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from jobagent.facts import FactBase
from jobagent.package_models import Claim, Job, Lang, ResumeDraft, RoleType
from jobagent.package_rules import CUT_ORDER, EMPLOYER_NAMES, EMPLOYER_NAMES_EN, REQUIRED


class PackageError(RuntimeError):
    """Пакет отклика нельзя собрать (резюме не помещается, нет данных и т. п.)."""


@dataclass(frozen=True)
class Contacts:
    email: str
    linkedin: str  # ссылка на профиль
    telegram: str  # ссылка
    location_line: str


LOCATION_LINE: dict[Lang, str] = {
    "en": "UTC+3 · Remote · Open to relocation · English C1 · Russian native · Spanish B1",
    "ru": "UTC+3 · Удалённо · Готов к переезду · Английский C1 · Русский родной · Испанский B1",
}

# ---------- шрифты ----------

_FONT_DIR = Path(__file__).parent / "fonts"
FONT = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"
FONT_ITALIC = "DejaVuSans-Oblique"


def _register_fonts() -> None:
    if FONT in pdfmetrics.getRegisteredFontNames():
        return
    for name in (FONT, FONT_BOLD, FONT_ITALIC):
        pdfmetrics.registerFont(TTFont(name, str(_FONT_DIR / f"{name}.ttf")))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD, italic=FONT_ITALIC, boldItalic=FONT_BOLD)


_register_fonts()

# ---------- стили ----------

BLUE = HexColor("#1a73e8")
GREY = HexColor("#555555")


def _leading(font_size: float, original: float) -> float:
    """Не меньше исходного и не меньше 1,25 × кегля (с округлением вверх до 0,1)."""
    minimal = math.ceil(round(font_size * 1.25 * 10, 6)) / 10
    return max(original, minimal)


def _style(name: str, font: str, size: float, leading: float, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, fontName=font, fontSize=size, leading=_leading(size, leading), **kw)


# Размеры шрифта и исходный интервал из applications_2026-10-01/build_cvs.py.
STYLES: dict[str, ParagraphStyle] = {
    "name": _style("name", FONT_BOLD, 22, 26),
    "headline": _style("headline", FONT, 10.5, 14, textColor=BLUE),
    "contacts": _style("contacts", FONT, 9, 12, textColor=GREY),
    "summary": _style("summary", FONT, 9.5, 13),
    "section": _style("section", FONT_BOLD, 12.5, 16, textColor=BLUE),
    "company": _style("company", FONT_BOLD, 10.5, 13),
    "meta": _style("meta", FONT_ITALIC, 8.5, 11, textColor=GREY, alignment=TA_RIGHT),
    "bullet": _style("bullet", FONT, 9.3, 12.4, leftIndent=12, bulletIndent=3, alignment=TA_LEFT),
    "body": _style("body", FONT, 9.3, 12.4),
}

MARGIN_X = 16 * mm
MARGIN_Y = 11 * mm
DATES_WIDTH = 42 * mm
FRAME_PAD = 6

NAME: dict[Lang, str] = {"en": "Alexander Bachinin", "ru": "Александр Бачинин"}
SECTIONS: dict[Lang, dict[str, str]] = {
    "en": {"summary": "Summary", "experience": "Experience", "skills": "Skills", "education": "Education"},
    "ru": {"summary": "О себе", "experience": "Опыт", "skills": "Навыки", "education": "Образование"},
}
EDUCATION: dict[Lang, str] = {
    "en": "RANEPA, Marketing; Translation and Translation Studies (2014)",
    "ru": "РАНХиГС, маркетинг; «Перевод и переводоведение» (2014)",
}

_MONTHS_RU = {
    "Jan": "янв.", "Feb": "февр.", "Mar": "март", "Apr": "апр.", "May": "май", "Jun": "июнь",
    "Jul": "июль", "Aug": "авг.", "Sep": "сент.", "Oct": "окт.", "Nov": "нояб.", "Dec": "дек.",
}


# ---------- тексты ----------


def employer_name(code: str, lang: Lang) -> str:
    if lang == "en" and code in EMPLOYER_NAMES_EN:
        return EMPLOYER_NAMES_EN[code]
    return EMPLOYER_NAMES.get(code, code)


def job_dates(code: str, fb: FactBase, lang: Lang) -> str:
    """Даты места работы из заголовка файла опыта: «Jan 2024 – Aug 2025»."""
    employer = fb.employers.get(code)
    if employer is None:
        raise PackageError(f"нет места работы {code} в файле опыта")
    dates = employer.heading.rsplit(" · ", 1)[-1]
    dates = re.sub(r"\s*\(.*?\)", "", dates).strip()
    if dates.endswith("наст. время"):
        dates = dates.replace("наст. время", "Present" if lang == "en" else "по наст. время")
    if lang == "ru":
        dates = re.sub(r"\b(" + "|".join(_MONTHS_RU) + r")\b", lambda m: _MONTHS_RU[m.group(1)], dates)
        dates = dates.replace("early", "начало")
    return dates


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    """Экранирование для Paragraph и **жирный** из Markdown."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _esc(text))


def _summary_text(draft: ResumeDraft) -> str:
    return " ".join(c.text for c in draft.summary)


# ---------- вёрстка ----------


def _section(title: str) -> list:
    return [
        Spacer(1, 4),
        Paragraph(_esc(title), STYLES["section"]),
        HRFlowable(width="100%", thickness=0.8, color=BLUE, spaceBefore=1, spaceAfter=4),
    ]


def _flowables(draft: ResumeDraft, fb: FactBase, contacts: Contacts, lang: Lang) -> list:
    s = STYLES
    titles = SECTIONS[lang]
    link = 'color="#1a73e8"'
    f: list = [
        Paragraph(NAME[lang], s["name"]),
        Spacer(1, 2),
        Paragraph(_inline(draft.headline), s["headline"]),
        Spacer(1, 2),
        Paragraph(
            f'{_esc(contacts.email)} | <link href="{_esc(contacts.linkedin)}" {link}>LinkedIn</link> | '
            f'<link href="{_esc(contacts.telegram)}" {link}>Telegram</link>',
            s["contacts"],
        ),
        Paragraph(_esc(contacts.location_line), s["contacts"]),
        Spacer(1, 4),
    ]
    f += _section(titles["summary"])
    f.append(Paragraph(_inline(_summary_text(draft)), s["summary"]))
    f += _section(titles["experience"])
    width = A4[0] - 2 * MARGIN_X - 2 * FRAME_PAD  # у фрейма platypus свой отступ по 6 pt с каждой стороны
    for job in draft.jobs:
        head = (
            f"{_esc(employer_name(job.employer, lang))} "
            f'<font name="{FONT}" color="#1a73e8">· {_esc(job.title)}</font>'
        )
        row = Table(
            [[Paragraph(head, s["company"]), Paragraph(_esc(job_dates(job.employer, fb, lang)), s["meta"])]],
            colWidths=[width - DATES_WIDTH, DATES_WIDTH],
        )
        row.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        f.append(row)
        f += [Paragraph(_inline(b.text), s["bullet"], bulletText="•") for b in job.bullets]
        f.append(Spacer(1, 4))
    f += _section(titles["skills"])
    f += [Paragraph(_inline(c.text), s["body"]) for c in draft.skills]
    f += _section(titles["education"])
    f.append(Paragraph(_esc(EDUCATION[lang]), s["body"]))
    return f


def render_pdf(draft: ResumeDraft, fb: FactBase, contacts: Contacts, lang: Lang) -> tuple[bytes, int]:
    """PDF резюме и число страниц."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X, topMargin=MARGIN_Y, bottomMargin=MARGIN_Y,
        title=f"{NAME[lang]}, {draft.headline.split('|')[0].strip()}", author=NAME[lang], invariant=1,
    )
    doc.build(_flowables(draft, fb, contacts, lang))
    return buf.getvalue(), doc.page


# ---------- подгонка ----------


def _replace_job(draft: ResumeDraft, index: int, job: Job | None) -> ResumeDraft:
    jobs = list(draft.jobs)
    if job is None:
        del jobs[index]
    else:
        jobs[index] = job
    return draft.model_copy(update={"jobs": jobs})


def _trim_first(draft: ResumeDraft, code: str) -> ResumeDraft | None:
    """Оставляет у места работы только первый пункт; None, если резать нечего."""
    for i, job in enumerate(draft.jobs):
        if job.employer == code and len(job.bullets) > 1:
            return _replace_job(draft, i, job.model_copy(update={"bullets": job.bullets[:1]}))
    return None


def _drop(draft: ResumeDraft, code: str, role_type: RoleType) -> ResumeDraft | None:
    if code in REQUIRED[role_type]:
        return None
    for i, job in enumerate(draft.jobs):
        if job.employer == code:
            return _replace_job(draft, i, None)
    return None


def _trim_long_steps(draft: ResumeDraft):
    """Шаги trim_long: у мест с 4 пунктами убрать последний, по одному месту за шаг, с конца состава."""
    for i in range(len(draft.jobs) - 1, -1, -1):
        job = draft.jobs[i]
        if len(job.bullets) >= 4:
            yield f"trim_long:{job.employer}", i


def fit_one_page(
    draft: ResumeDraft, fb: FactBase, contacts: Contacts, lang: Lang, role_type: RoleType
) -> tuple[ResumeDraft, bytes, list[str]]:
    """Подгоняет резюме под одну страницу, сокращая содержание по CUT_ORDER. Стили не трогает.

    Возвращает подогнанный черновик, PDF и выполненные шаги (шаг, который ничего не изменил, не записывается).
    """
    steps: list[str] = []
    pdf, pages = render_pdf(draft, fb, contacts, lang)
    for step in CUT_ORDER:
        if pages <= 1:
            break
        kind, _, code = step.partition(":")
        if kind == "trim":
            cut = _trim_first(draft, code)
            if cut is not None:
                draft, steps = cut, steps + [step]
                pdf, pages = render_pdf(draft, fb, contacts, lang)
        elif kind == "trim_long":
            while pages > 1:
                nxt = next(iter(_trim_long_steps(draft)), None)
                if nxt is None:
                    break
                name, i = nxt
                job = draft.jobs[i]
                draft = _replace_job(draft, i, job.model_copy(update={"bullets": job.bullets[:-1]}))
                steps.append(name)
                pdf, pages = render_pdf(draft, fb, contacts, lang)
        elif kind == "drop":
            cut = _drop(draft, code, role_type)
            if cut is not None:
                draft, steps = cut, steps + [step]
                pdf, pages = render_pdf(draft, fb, contacts, lang)
    if pages > 1:
        raise PackageError("не помещается на страницу")
    return draft, pdf, steps


# ---------- Markdown ----------


def _ids(claim: Claim) -> str:
    return " ".join(f"<!-- {fact_id} -->" for fact_id in claim.facts)


def to_markdown(draft: ResumeDraft, fb: FactBase, contacts: Contacts, lang: Lang = "en") -> str:
    """Markdown-исходник резюме для хранения; номера фактов идут HTML-комментариями после текста."""
    titles = SECTIONS[lang]
    lines = [
        f"# {NAME[lang]}",
        f"**{draft.headline}**",
        contacts.location_line,
        f"{contacts.email} | [LinkedIn]({contacts.linkedin}) | [Telegram]({contacts.telegram})",
        "",
        f"## {titles['summary']}",
    ]
    lines += [f"{c.text} {_ids(c)}".rstrip() for c in draft.summary]
    lines += ["", f"## {titles['experience']}"]
    for job in draft.jobs:
        lines += ["", f"### {employer_name(job.employer, lang)} · {job.title} · {job_dates(job.employer, fb, lang)}"]
        lines += [f"- {b.text} {_ids(b)}".rstrip() for b in job.bullets]
    lines += ["", f"## {titles['skills']}"]
    lines += [f"- {c.text} {_ids(c)}".rstrip() for c in draft.skills]
    lines += ["", f"## {titles['education']}", EDUCATION[lang], ""]
    return "\n".join(lines)

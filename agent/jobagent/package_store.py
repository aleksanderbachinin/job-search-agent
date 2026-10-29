"""Версии пакетов отклика: строка в таблице packages и папка `root/<slug>/v<N>/` с файлами.

Файлы (папки 0o700, файлы 0o600: внутри текст резюме и письма, правки и контакты):
  resume.json, experience_map.json — черновик резюме и карта требований как есть;
  resume.md, letter.md, experience_map.md — для чтения глазами;
  <filename>.pdf — резюме;
  check.json — всё остальное из BuiltPackage (язык, тип роли, письмо, removed и warnings с деталями);
  feedback.txt — правка Александра, если была.

В события идут только коды и числа: `Violation.detail` и warnings содержат сгенерированный текст,
поэтому подробности лежат на диске, а в `package_ready` — пути с кодами и число предупреждений.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .db import Database
from .factcheck import Violation
from .facts import FactBase
from .package import BuiltPackage
from .package_models import LetterDraft, RequirementMap, ResumeDraft
from .pdf import LOCATION_LINE, Contacts, to_markdown

_SLUG = re.compile(r"[\w][\w.-]*")
_STATUS_LABEL = {"closed": "закрыто", "partial": "частично", "gap": "пробел"}


def _write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    path.chmod(0o600)  # на случай, если файл уже был с другими правами


def _write_text(path: Path, text: str) -> None:
    _write(path, text.encode("utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    _write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def map_to_markdown(req_map: RequirementMap) -> str:
    """Требования вакансии и чем они закрыты; пробелы честно отдельным списком."""
    lines = ["# Карта требований", "", f"Тип вакансии: {req_map.role_type}", ""]
    for r in req_map.requirements:
        facts = ", ".join(r.facts) if r.facts else "нет в опыте"
        lines.append(f"- [{_STATUS_LABEL[r.status]}] {r.text} ({facts})")
    gaps = [r.text for r in req_map.requirements if r.status == "gap"]
    lines += ["", "## Пробелы"]
    lines += [f"- {t}" for t in gaps] or ["Нет."]
    return "\n".join(lines) + "\n"


def write_files(path: Path, built: BuiltPackage, fb: FactBase, contacts: Contacts) -> None:
    """Файлы для чтения глазами: PDF, resume.md, letter.md, experience_map.md. Папка — только для владельца.

    Строка места и языков в resume.md — по языку пакета, как в PDF.
    """
    if Path(built.filename).name != built.filename:
        raise ValueError("недопустимое имя PDF")
    _private_dir(path)
    contacts = replace(contacts, location_line=LOCATION_LINE[built.lang])
    _write(path / built.filename, built.pdf)
    _write_text(path / "resume.md", to_markdown(built.resume, fb, contacts, built.lang))
    _write_text(path / "letter.md", built.letter_text)
    _write_text(path / "experience_map.md", map_to_markdown(built.req_map))


class PackageStore:
    def __init__(self, db: Database, root: Path, fb: FactBase, contacts: Contacts):
        self.db = db
        self.root = root
        self.fb = fb  # нужен для дат мест работы в resume.md
        self.contacts = contacts

    def _dir(self, slug: str, version: int) -> Path:
        if not _SLUG.fullmatch(slug) or slug.startswith(".."):
            raise ValueError("недопустимый slug")
        return self.root / slug / f"v{version}"

    async def start(self, slug: str) -> int:
        self._dir(slug, 1)  # проверка slug до записи в базу
        return await self.db.create_package(slug)

    async def save(self, slug: str, version: int, built: BuiltPackage, feedback: str | None) -> Path:
        if Path(built.filename).name != built.filename:
            raise ValueError("недопустимое имя PDF")
        path = self._dir(slug, version)
        for d in (self.root, path.parent):
            _private_dir(d)
        write_files(path, built, self.fb, self.contacts)
        _write_json(path / "resume.json", built.resume.model_dump())
        _write_json(path / "experience_map.json", built.req_map.model_dump())
        _write_json(
            path / "check.json",
            {
                "lang": built.lang,
                "role_type": built.role_type,
                "filename": built.filename,
                "cut_steps": built.cut_steps,
                "removed": [asdict(v) for v in built.removed],
                "warnings": built.warnings,
                "letter": built.letter.model_dump(),
            },
        )
        if feedback:
            _write_text(path / "feedback.txt", feedback)
        await self.db.set_package(slug, version, status="ready", path=str(path))
        await self.db.log_event(
            "package_ready",
            slug,
            version=version,
            removed=[{"path": v.path, "code": v.code} for v in built.removed],
            warnings=len(built.warnings),
            cut_steps=built.cut_steps,
        )
        return path

    async def fail(self, slug: str, version: int, code: str) -> None:
        """code — короткий код (composition, timeout, api_error), не текст исключения."""
        await self.db.set_package(slug, version, status="failed")
        await self.db.log_event("package_failed", slug, version=version, code=code)

    async def load(self, slug: str, version: int) -> BuiltPackage:
        path = self._dir(slug, version)
        check = json.loads((path / "check.json").read_text(encoding="utf-8"))
        filename = check["filename"]
        if Path(filename).name != filename:
            raise ValueError("недопустимое имя PDF")
        return BuiltPackage(
            lang=check["lang"],
            role_type=check["role_type"],
            req_map=RequirementMap.model_validate_json((path / "experience_map.json").read_bytes()),
            resume=ResumeDraft.model_validate_json((path / "resume.json").read_bytes()),
            letter=LetterDraft.model_validate(check["letter"]),
            letter_text=(path / "letter.md").read_text(encoding="utf-8"),
            pdf=(path / filename).read_bytes(),
            filename=filename,
            removed=[Violation(**v) for v in check["removed"]],
            warnings=check["warnings"],
            cut_steps=check["cut_steps"],
        )

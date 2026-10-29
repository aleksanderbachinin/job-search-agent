"""Настройки сервиса из переменных окружения (и файла .env рядом с agent/)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

AGENT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(AGENT_DIR / ".env")


class ConfigError(RuntimeError):
    pass


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


@dataclass(frozen=True)
class Settings:
    hirify_api_key: str = ""
    hirify_webhook_secret: str = ""
    telegram_bot_token: str = ""
    telegram_owner_id: int = 0
    # Пусто = RU-проверка по LinkedIn выключена, Sonnet судит только по тексту вакансии.
    scrapecreators_api_key: str = ""
    # Пусто = все активные фильтры аккаунта.
    feed_ids: list[int] = field(default_factory=list)
    db_path: Path = AGENT_DIR / "data" / "jobagent.sqlite3"
    # Папка с search_criteria.md и мастер-резюме.
    knowledge_dir: Path = AGENT_DIR.parent
    triage_model: str = "claude-haiku-5-5"
    scoring_model: str = "claude-sonnet-5-5"
    scoring_effort: str = "medium"
    # Отсев Haiku по полному тексту перед Sonnet. PREFILTER=0 выключает.
    prefilter: bool = True
    # Пакет отклика: генерация резюме и письма, проверка смысла утверждений.
    package_model: str = "claude-opus-5-5"
    package_check_model: str = "claude-haiku-5-5"
    # Без PACKAGES_DIR — рядом с базой: на сервере systemd разрешает запись только в папку базы.
    packages_dir: Path = AGENT_DIR / "data" / "packages"
    # Контакты в резюме и подписи письма.
    candidate_email: str = ""
    candidate_linkedin: str = ""
    candidate_telegram: str = ""
    min_score: int = 4
    max_age_days: int = 7
    reconcile_hours: float = 12.0
    reconcile_pages: int = 2
    reconcile_per_page: int = 50
    webhook_tolerance_sec: int = 900
    host: str = "0.0.0.0"
    port: int = 8080

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        d = cls()
        db_path = Path(env.get("DB_PATH") or d.db_path)
        return cls(
            hirify_api_key=env.get("HIRIFY_API_KEY", ""),
            hirify_webhook_secret=env.get("HIRIFY_WEBHOOK_SECRET", ""),
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_owner_id=int(env.get("TELEGRAM_OWNER_ID") or 0),
            scrapecreators_api_key=env.get("SCRAPECREATORS_API_KEY", ""),
            feed_ids=_int_list(env.get("HIRIFY_FEED_IDS", "")),
            db_path=db_path,
            knowledge_dir=Path(env.get("KNOWLEDGE_DIR") or d.knowledge_dir),
            triage_model=env.get("TRIAGE_MODEL", d.triage_model),
            scoring_model=env.get("SCORING_MODEL", d.scoring_model),
            scoring_effort=env.get("SCORING_EFFORT", d.scoring_effort),
            prefilter=env.get("PREFILTER", "1").strip().lower() not in ("0", "false", "no", "off"),
            package_model=env.get("PACKAGE_MODEL", d.package_model),
            package_check_model=env.get("PACKAGE_CHECK_MODEL", d.package_check_model),
            packages_dir=Path(env.get("PACKAGES_DIR") or db_path.parent / "packages"),
            candidate_email=env.get("CANDIDATE_EMAIL", ""),
            candidate_linkedin=env.get("CANDIDATE_LINKEDIN", ""),
            candidate_telegram=env.get("CANDIDATE_TELEGRAM", ""),
            min_score=int(env.get("MIN_SCORE") or d.min_score),
            max_age_days=int(env.get("MAX_AGE_DAYS") or d.max_age_days),
            reconcile_hours=float(env.get("RECONCILE_HOURS") or d.reconcile_hours),
            reconcile_pages=int(env.get("RECONCILE_PAGES") or d.reconcile_pages),
            webhook_tolerance_sec=int(env.get("WEBHOOK_TOLERANCE_SEC") or d.webhook_tolerance_sec),
            host=env.get("HOST", d.host),
            port=int(env.get("PORT") or d.port),
        )

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env_names = ", ".join(n.upper() for n in missing)
            raise ConfigError(f"Не заданы переменные окружения: {env_names}")

"""Клиент Claude: напрямую через Anthropic API или через OpenRouter.

У OpenRouter есть совместимый с Anthropic эндпоинт /v1/messages, поэтому код шагов не меняется:
тот же AsyncAnthropic, другой base_url и ключ. Различия два:
  - модели называются иначе: claude-sonnet-5-5 → anthropic/claude-sonnet-5.5;
  - кончились кредиты — ответ 402, а не 400 «credit balance is too low».
"""

from __future__ import annotations

import re
import time

import anthropic

OPENROUTER_BASE_URL = "https://openrouter.ai/api"

_ANTHROPIC_ID = re.compile(r"^(claude-[a-z]+)-(\d+)(?:-(\d+))?$")


def openrouter_model(name: str) -> str:
    """claude-sonnet-5-5 → anthropic/claude-sonnet-5.5; уже заданное в формате OpenRouter не трогаем."""
    if "/" in name:
        return name
    m = _ANTHROPIC_ID.match(name)
    if not m:
        return name
    family, major, minor = m.groups()
    return f"anthropic/{family}-{major}" + (f".{minor}" if minor else "")


def canonical_model(name: str) -> str:
    """Обратное преобразование для таблицы цен: anthropic/claude-sonnet-5.5 → claude-sonnet-5-5."""
    return name.removeprefix("anthropic/").replace(".", "-")


def make_client(openrouter_api_key: str = "", max_retries: int = 2) -> anthropic.AsyncAnthropic:
    """С ключом OpenRouter — клиент к OpenRouter, иначе обычный (ключ ANTHROPIC_API_KEY из окружения).

    Ключ OpenRouter передаётся явно: так ANTHROPIC_API_KEY из .env в OpenRouter не уходит.
    """
    if openrouter_api_key:
        return anthropic.AsyncAnthropic(
            api_key=openrouter_api_key, base_url=OPENROUTER_BASE_URL, max_retries=max_retries
        )
    return anthropic.AsyncAnthropic(max_retries=max_retries)


def out_of_credits(e: anthropic.APIStatusError) -> bool:
    """Кончился баланс: у Anthropic 400 с «credit balance», у OpenRouter 402."""
    if e.status_code == 402:
        return True
    text = str(e).lower()
    return "credit balance" in text or "insufficient credits" in text


RATE_LIMIT_MIN_WAIT = 60
RATE_LIMIT_MAX_WAIT = 12 * 3600


def rate_limit_wait(e: anthropic.RateLimitError) -> float:
    """Сколько ждать после 429. OpenRouter пишет время сброса в X-RateLimit-Reset (мс от эпохи).

    Минутный лимит бесплатных моделей сбрасывается быстро, дневной — до полуночи UTC.
    """
    raw = e.response.headers.get("X-RateLimit-Reset") if e.response is not None else None
    try:
        wait = int(raw) / 1000 - time.time() if raw else RATE_LIMIT_MIN_WAIT
    except ValueError:
        wait = RATE_LIMIT_MIN_WAIT
    return max(RATE_LIMIT_MIN_WAIT, min(wait, RATE_LIMIT_MAX_WAIT))

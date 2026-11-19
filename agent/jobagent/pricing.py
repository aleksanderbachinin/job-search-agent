"""Цены Claude API в долларах за 1M токенов и стоимость одного вызова по его Usage.

Источник: скилл claude-api (Claude Code 2.1.309, таблица моделей от 2026-10-06 и
shared/prompt-caching.md, раздел «Economics»), сверено 2026-10-28:
  - Opus 5.5: ввод $4, вывод $20, чтение кеша $0.20;
  - Sonnet 5.5: ввод $2, вывод $10, чтение кеша $0.20;
  - Haiku 5.5: ввод $0.10, вывод $0.50 (промпт до 100K токенов); чтение кеша для неё отдельно
    не названо, взято общее правило 0,1 × ввода;
  - запись в кеш с TTL 1 час — 2 × ввода. Сервис кеширует только на час (CACHE_1H), поэтому
    cache_write считаем по этой цене.
Haiku 5.5 с промптом больше 100K стоит дороже ($0.50 / $2.50); у нас таких промптов нет.
Меняются цены или модели — правь таблицу и дату сверки.
Через OpenRouter цены моделей те же (сверено 2026-11-19 по openrouter.ai/api/v1/models),
названия приводятся к ключам таблицы через llm.canonical_model.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, NamedTuple

from .llm import canonical_model


class Price(NamedTuple):
    input: float
    output: float
    cache_read: float
    cache_write: float


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20, cache_write=8.00),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.20, cache_write=4.00),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01, cache_write=0.20),
}


def usage_cost(usage: Any) -> float | None:
    """Стоимость вызова в долларах. usage — Usage или его asdict (как в событии llm).

    None, если модели нет в PRICES: её задали переменной окружения, а цену не внесли.
    """
    u = asdict(usage) if is_dataclass(usage) else usage
    if (u.get("model") or "").endswith(":free"):
        return 0.0  # бесплатные модели OpenRouter
    price = PRICES.get(canonical_model(u.get("model") or ""))
    if price is None:
        return None
    return (
        (u.get("input_tokens") or 0) * price.input
        + (u.get("output_tokens") or 0) * price.output
        + (u.get("cache_read_tokens") or 0) * price.cache_read
        + (u.get("cache_write_tokens") or 0) * price.cache_write
    ) / 1_000_000

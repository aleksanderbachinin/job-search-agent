"""Воркер очереди: правила → Haiku по карточке → полный текст → Haiku по тексту → Sonnet → карточка в бот."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Protocol

import anthropic

from . import rules
from .db import Database, Vacancy, company_key
from .hirify import HirifyClient, HirifyError
from .llm import out_of_credits
from .research import skip_reason
from .scoring import Scorer, ScoringError

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
IDLE_WAIT_SEC = 60
# Кончился баланс (Anthropic или OpenRouter): вакансия остаётся в очереди, попытка не тратится, проверяем раз в час.
CREDIT_WAIT_SEC = 3600


class CardSink(Protocol):
    async def send_card(self, v: Vacancy) -> int: ...
    async def alert(self, text: str) -> None: ...


class Research(Protocol):
    credits_used: int

    async def research(self, company: str | None, description: str = "") -> dict | None: ...


class Pipeline:
    def __init__(
        self,
        db: Database,
        hirify: HirifyClient,
        scorer: Scorer,
        sink: CardSink,
        min_score: int,
        max_age_days: int,
        research: Research | None = None,
        prefilter: bool = True,
    ):
        self.db = db
        self.hirify = hirify
        self.scorer = scorer
        self.sink = sink
        self.min_score = min_score
        self.max_age_days = max_age_days
        self.research = research
        self.prefilter = prefilter
        self.wake = asyncio.Event()
        self.no_credits = False

    async def run_forever(self) -> None:
        while True:
            v = await self.db.next_queued()
            if v is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=IDLE_WAIT_SEC)
                except TimeoutError:
                    pass
                continue
            pause = await self.process_safely(v)
            if pause:
                await asyncio.sleep(pause)

    async def process_safely(self, v: Vacancy) -> float:
        """Обрабатывает вакансию. Возвращает паузу в секундах перед следующей (при лимитах)."""
        try:
            await self.process(v)
            if self.no_credits:
                self.no_credits = False
                await self.sink.alert("Баланс Claude API снова есть, продолжаю разбирать очередь.")
            return 0
        except anthropic.BadRequestError as e:
            if not out_of_credits(e):
                log.exception("Ошибка обработки %s", v.slug)
                return await self._fail(v, e)
            return await self._no_credits(v)
        except HirifyError as e:
            if e.is_limit:
                wait = float(e.retry_after or 600)
                log.warning("Лимит Hirify (%s), пауза %.0f с", e.code, wait)
                if wait > 3600:
                    await self.sink.alert(f"Лимит Hirify исчерпан ({e.code}), продолжу через {wait / 3600:.1f} ч")
                return wait
            if e.needs_person:
                await self.sink.alert(f"Hirify требует действия в аккаунте: {e}")
                return 3600
            if e.is_transient:
                log.warning("Hirify временно недоступен (%s), пауза 2 мин", e)
                return 120
            return await self._fail(v, e)
        except anthropic.RateLimitError:
            return 60
        except (anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            log.warning("Claude API недоступен: %s", e)
            return 60
        except anthropic.APIStatusError as e:
            # OpenRouter без кредитов отвечает 402: у SDK для него нет отдельного класса.
            if out_of_credits(e):
                return await self._no_credits(v)
            log.exception("Ошибка обработки %s", v.slug)
            return await self._fail(v, e)
        except Exception as e:
            log.exception("Ошибка обработки %s", v.slug)
            return await self._fail(v, e)

    async def _no_credits(self, v: Vacancy) -> float:
        log.warning("Кончился баланс LLM API, %s остаётся в очереди", v.slug)
        if not self.no_credits:
            self.no_credits = True
            await self.sink.alert(
                "Кончился баланс Claude API (Anthropic или OpenRouter). Вакансии ждут в очереди, "
                "проверяю раз в час. После пополнения продолжу сам."
            )
        return CREDIT_WAIT_SEC

    async def _fail(self, v: Vacancy, e: Exception) -> float:
        attempts = v.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            await self.db.update(v.slug, status="error", attempts=attempts, reason=str(e)[:500])
            await self.db.log_event("error", v.slug, error=str(e)[:500])
            await self.sink.alert(f"Не смог обработать «{v.title}» после {attempts} попыток: {e}")
        else:
            await self.db.update(v.slug, attempts=attempts)
        return 5

    async def reject(self, v: Vacancy, stage: str, reason: str) -> None:
        await self.db.update(v.slug, status="rejected", stage=stage, reason=reason)
        await self.db.log_event("rejected", v.slug, stage=stage, reason=reason)
        log.info("rejected [%s] %s: %s", stage, v.title, reason)

    async def company_skipped(self, company: str | None) -> bool:
        return bool(company) and company_key(company) in await self.db.skipped_companies()

    async def process(self, v: Vacancy) -> None:
        if reason := rules.check_card(v.card, relocation=rules.relocation_feed(v.feeds)):
            stage = "already" if "откликался" in reason or "трекере" in reason else "rules"
            return await self.reject(v, stage, reason)

        triage, usage = await self.scorer.triage(v.card)
        await self.db.log_event("llm", v.slug, step="triage", **asdict(usage))
        if not triage.relevant:
            return await self.reject(v, "triage", triage.reason)

        if v.vacancy_id is None:
            raise ValueError(f"Нет vacancy_id для {v.slug}")
        detail = await self.hirify.read_vacancy(v.vacancy_id)
        company = detail.get("company")
        await self.db.update(v.slug, detail=detail, company=company)
        if rejection := rules.check_detail(detail, self.max_age_days):
            return await self.reject(v, *rejection)
        # Hirify скрывает компанию после пропуска, но вакансии, уже стоящие в очереди, доходят сюда.
        if await self.company_skipped(company):
            return await self.reject(v, "company", f"компания {company} пропущена как неинтересная")

        # До LinkedIn и Sonnet: явные стоп-факторы дешевле отсечь Haiku по тексту.
        if self.prefilter:
            pre, usage = await self.scorer.prefilter(detail, v.feeds)
            await self.db.log_event("llm", v.slug, step="prefilter", **asdict(usage))
            if not pre.relevant:
                return await self.reject(v, "prefilter", pre.reason)

        if self.research and (skip := skip_reason(detail)):
            await self.db.log_event("research", v.slug, skipped=skip)
        elif self.research:
            before = self.research.credits_used
            if found := await self.research.research(detail.get("company"), detail.get("description") or ""):
                detail["linkedin_research"] = found
                await self.db.update(v.slug, detail=detail)
            await self.db.log_event(
                "research", v.slug, found=bool(found), credits=self.research.credits_used - before
            )

        assessment, usage = await self.scorer.assess(detail, v.feeds)
        await self.db.log_event("llm", v.slug, step="scoring", **asdict(usage))
        a = assessment.model_dump()
        await self.db.update(v.slug, assessment=a, score=assessment.score)
        if assessment.score < self.min_score:
            return await self.reject(v, "score", f"{assessment.score}/5: {assessment.verdict}")
        # Стоп-фактор: на иностранные команды Александр не откликается при любой оценке.
        if assessment.ru_label == "foreign":
            return await self.reject(v, "foreign", f"{assessment.score}/5, иностранная команда: {assessment.ru_summary}")

        # Ещё раз: пока шла оценка, Александр мог пропустить другую карточку этой компании.
        if await self.company_skipped(company):
            return await self.reject(v, "company", f"компания {company} пропущена как неинтересная")

        fresh = await self.db.get(v.slug)
        assert fresh is not None
        message_id = await self.sink.send_card(fresh)
        await self.db.update(v.slug, status="shown", tg_message_id=message_id)
        await self.db.log_event("shown", v.slug, score=assessment.score)
        log.info("shown %s/5 %s", assessment.score, v.title)

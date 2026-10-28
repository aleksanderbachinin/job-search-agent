"""Структуры ответов модели при сборке пакета отклика."""

from typing import Literal

from pydantic import BaseModel

RoleType = Literal["builder", "growth", "product"]
Lang = Literal["en", "ru"]


class Requirement(BaseModel):
    """Требование вакансии и чем оно закрыто в опыте."""

    text: str
    facts: list[str]
    status: Literal["closed", "partial", "gap"]


class RequirementMap(BaseModel):
    role_type: RoleType
    requirements: list[Requirement]


class Claim(BaseModel):
    """Утверждение об опыте со ссылками на номера фактов."""

    text: str
    facts: list[str]
    requirement: str | None = None  # текст требования из карты, которое закрывает утверждение


class Job(BaseModel):
    employer: str
    title: str
    bullets: list[Claim]


class ResumeDraft(BaseModel):
    headline: str
    summary: list[Claim]
    jobs: list[Job]
    skills: list[Claim]


class LetterSentence(Claim):
    paragraph: int


class LetterDraft(BaseModel):
    """Письмо без подписи: подпись добавляет код. Утверждения без facts — хук о компании и призыв."""

    subject: str | None
    salutation: str
    sentences: list[LetterSentence]
    closing: str

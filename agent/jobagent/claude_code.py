"""Запросы eval через Claude Code (`claude -p`): по подписке, а не за счёт ключа API.

Повторяет ту часть AsyncAnthropic, которой пользуется Scorer: messages.parse и
beta.messages.parse. Каждый вызов — отдельный процесс `claude -p` со своим system prompt,
без инструментов, настроек и MCP; ответ приходит по той же JSON-схеме, что и через API.

Только для локальных проверок промптов. Сервис на сервере работает через API.
Отличия от API: кешем промпта управляет Claude Code, `max_tokens` и fallbacks не передаются.
Сравнивать прогоны между собой стоит внутри одного бэкенда.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from types import SimpleNamespace
from typing import Any

from anthropic import transform_schema
from pydantic import BaseModel

# Без них `claude -p` возьмёт ключ API из окружения (его кладёт туда agent/.env) и спишет кредиты.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
TIMEOUT_SEC = 600
LOGIN_HINT = "Войдите в Claude Code по подписке: в терминале `claude auth login`, затем повторите прогон."


class ClaudeCodeError(RuntimeError):
    pass


class ClaudeCodeLimitError(ClaudeCodeError):
    """Лимит подписки или вход: следующие запросы прогона тоже не пройдут."""


def system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(block["text"] for block in system)


def user_text(messages: list[dict[str, Any]]) -> str:
    if len(messages) != 1 or messages[0].get("role") != "user":
        raise ValueError("ClaudeCodeClient принимает ровно одно сообщение пользователя")
    content = messages[0]["content"]
    if isinstance(content, str):
        return content
    return "\n\n".join(block["text"] for block in content if block.get("type") == "text")


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}


def is_limit_error(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in ("authenticate", "login", "log in", "usage limit", "hit your limit", "rate limit"))


def build_command(binary: str, model: str, system: str, schema: dict[str, Any], effort: str | None) -> list[str]:
    cmd = [
        binary, "-p",
        "--model", model,
        "--system-prompt", system,
        "--json-schema", json.dumps(schema, ensure_ascii=False),
        "--output-format", "json",
        # Без инструментов, сохранения сессии, пользовательских настроек и MCP: только ответ модели.
        "--tools", "",
        "--no-session-persistence",
        "--setting-sources", "",
        "--strict-mcp-config",
    ]
    if effort:
        cmd += ["--effort", effort]
    return cmd


def parse_result(raw: str, model: str, output_format: type[BaseModel]) -> SimpleNamespace:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ClaudeCodeError(f"claude -p вернул не JSON: {raw[:300]}") from e
    if data.get("is_error"):
        text = str(data.get("result") or data.get("terminal_reason") or "ошибка без текста")
        if is_limit_error(text):
            raise ClaudeCodeLimitError(f"{text}. {LOGIN_HINT}" if "authenticat" in text.lower() else text)
        raise ClaudeCodeError(text[:500])
    u = data.get("usage") or {}
    usage = SimpleNamespace(
        input_tokens=u.get("input_tokens") or 0,
        output_tokens=u.get("output_tokens") or 0,
        cache_read_input_tokens=u.get("cache_read_input_tokens") or 0,
        cache_creation_input_tokens=u.get("cache_creation_input_tokens") or 0,
    )
    structured = data.get("structured_output")
    parsed = output_format.model_validate(structured) if structured is not None else None
    used = list((data.get("modelUsage") or {}).keys())
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason="end_turn" if parsed is not None else data.get("stop_reason"),
        usage=usage,
        model=used[0] if len(used) == 1 else model,
    )


class _Messages:
    def __init__(self, client: ClaudeCodeClient):
        self._client = client

    async def parse(
        self,
        *,
        model: str,
        system: Any,
        messages: list[dict[str, Any]],
        output_format: type[BaseModel],
        output_config: dict[str, Any] | None = None,
        **_ignored: Any,  # max_tokens, betas, fallbacks: в `claude -p` их не передать
    ) -> SimpleNamespace:
        effort = (output_config or {}).get("effort")
        return await self._client.run(model, system_text(system), user_text(messages), output_format, effort)


class ClaudeCodeClient:
    """Подставляется в Scorer(client=...) вместо anthropic.AsyncAnthropic."""

    def __init__(self, binary: str | None = None, timeout: float = TIMEOUT_SEC):
        found = binary or shutil.which("claude")
        if not found:
            raise ClaudeCodeError("Не найден `claude` (Claude Code CLI) в PATH")
        self.binary = found
        self.timeout = timeout
        self.messages = _Messages(self)
        self.beta = SimpleNamespace(messages=self.messages)
        # После лимита или ошибки входа остальные запросы прогона не запускаются.
        self.stopped: str | None = None

    async def run(
        self, model: str, system: str, prompt: str, output_format: type[BaseModel], effort: str | None
    ) -> SimpleNamespace:
        if self.stopped:
            raise ClaudeCodeLimitError(self.stopped)
        cmd = build_command(self.binary, model, system, transform_schema(output_format), effort)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ClaudeCodeError(f"claude -p не ответил за {self.timeout:.0f} с") from None
        if not out.strip():
            raise ClaudeCodeError(f"claude -p завершился с кодом {proc.returncode}: {err.decode(errors='replace')[:300]}")
        try:
            return parse_result(out.decode("utf-8"), model, output_format)
        except ClaudeCodeLimitError as e:
            self.stopped = str(e)
            raise

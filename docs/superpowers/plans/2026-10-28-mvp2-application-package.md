# MVP-2 «Пакет отклика»: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** По кнопке «📝 Подготовить отклик» в карточке бот присылает PDF-резюме на одну страницу и сопроводительное письмо, где каждое утверждение об опыте опирается на пронумерованные факты из файла опыта и проверено кодом.

**Architecture:** Файл опыта получает номера фактов, парсер строит из него базу фактов. Цепочка в коде: карта требований (Sonnet) → резюме и письмо структурой со ссылками на факты (Opus) → проверки кодом и узкая проверка смысла (Haiku) → одна пересборка с перечнем нарушений → удаление того, что не прошло → PDF с подгонкой под страницу. Сборка идёт фоновой задачей бота, пакет хранится на сервере по версиям, «Переделать» пересобирает с правкой Александра.

**Tech Stack:** Python 3.12, anthropic SDK (`messages.parse` с Pydantic), aiogram 3, aiosqlite, reportlab (новая зависимость), шрифт DejaVu Sans (кладётся в репозиторий), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-28-mvp2-application-package-design.md`. Читать вместе с планом.

## Global Constraints

- Пакет собирается только по нажатию кнопки, для вакансий со статусом `shown`. Наружу (работодателю, в Hirify) ничего не отправляется.
- Каждый пункт резюме, фраза summary, пункт навыков и утверждение письма об опыте ссылаются на номера фактов (`facts: [...]`). Утверждение без источника удаляется после одной пересборки и попадает в сводку «убрал: …».
- Письмо: нет символов `—` (U+2014) и `–` (U+2013) нигде, тема включительно; не больше 300 слов; нет фраз из таблицы «Avoid» `cover_letter_guide.md`.
- Язык резюме и письма: `ru`, если `detail.vacancy_language == "ru"`; любой другой указанный язык → `en`; не указан → `ru`, если доля кириллицы в `description` ≥ 0,3, иначе `en`.
- Состав мест работы по типу вакансии (`builder` / `growth` / `product`) — таблица из раздела 3 спецификации. Clowe, Mediagram, Mir Mining не берутся никогда. AEZAKMI обязателен всегда.
- Пунктов: 3–4 у первых трёх мест работы в порядке состава, 1–2 у остальных.
- Заголовок резюме только из списка позиционирований раздела «Профиль»; `CPO` запрещён.
- PDF ровно одна страница A4. Для каждого стиля `leading ≥ 1.25 × fontSize`. Сжимать стили ради страницы нельзя, только сокращать содержание.
- Имя PDF: `Bachinin_Alexander_<Company>_<Role>.pdf`, части очищены до `[A-Za-z0-9]` и `_`; компания скрыта → `Bachinin_Alexander_<Role>.pdf`.
- Telegram: текст сообщения ≤ 4096 символов, подпись к документу ≤ 1024.
- В логах и событиях нет текста вакансии, резюме, письма, правки Александра и контактов. Только коды, номера фактов, числа и длины.
- Модели: генерация `claude-opus-5-5`, карта требований — `scoring_model` из настроек (Sonnet), проверка смысла `claude-haiku-5-5`. Все три задаются переменными окружения.
- Разработка по `CLAUDE.md`, раздел «Как вести разработку»: сначала падающий тест; перед коммитом `cd agent && uv run pytest -q`; правка файла опыта — это правка промпта (eval до и после, раздел «Задача 1»).

## Review Focus

Входы, которые спецификация подразумевает, но не называет, и которые скорее всего ударят по Александру. Тест на каждый добавлен в задачу, которой он принадлежит.

1. **Перезапуск сервиса посреди сборки.** Пакет навсегда остаётся `building`, кнопка отвечает «уже собираю». Ожидаемо: при старте все `building` переходят в `failed`, кнопка снова работает. Тест в задаче 7.
2. **Правдивый пункт, переведённый на английский, отклоняется проверкой** (`1,000+` против `1 000+`, `halved` против `вдвое`, `1M+` против `1M+ просмотров`). Пакет теряет лучшие пункты. Ожидаемо: правдивые пункты ручного резюме AI Product Builder, сверенные с файлом опыта, проходят проверку кодом. Тест в задаче 4.
3. **Повторное нажатие и «Переделать» к старой версии.** Две сборки одной вакансии параллельно или правка, применённая не к той версии. Ожидаемо: пока идёт сборка, второе нажатие отвечает «уже собираю»; правка в ответ на запрос к версии N строится от версии N. Тест в задаче 8.
4. **Правка Александра с именем рекрутера или вилкой** («напиши Анне, что жду 5k»). Ожидаемо: текст правки только в папке пакета, в событиях только длина. Тест в задаче 8.
5. **Скрытая компания или спецсимволы в названии** (`company_masked`, `Acme & Co / R&D`). Ожидаемо: имя файла без `None` и без `/`, обращение без имени компании. Тест в задаче 5.

---

## Карта файлов

| Файл | Отвечает за |
|---|---|
| `experience/Bachinin_Alexander_Experience.md` (изменить) | Номера фактов `[CODE-N]` в конце каждого пункта |
| `agent/jobagent/facts.py` (создать) | Парсер файла опыта: факты, места работы, позиционирования, извлечение чисел и названий |
| `agent/jobagent/package_models.py` (создать) | Pydantic-структуры ответов модели: карта требований, резюме, письмо |
| `agent/jobagent/package_rules.py` (создать) | Состав мест работы по типу, вложения, порядок сокращения, запрещённые формулировки, стоп-фразы гайда |
| `agent/jobagent/textchecks.py` (создать) | Тире, подсчёт слов, стоп-фразы, язык текста, безопасное имя файла |
| `agent/jobagent/factcheck.py` (создать) | Проверки кодом: ссылки, принадлежность месту работы, числа, названия, запреты, пробелы, состав |
| `agent/jobagent/pdf.py` (создать) | Стили, шрифт, рендер `ResumeDraft` в PDF, подгонка под одну страницу, Markdown для хранения |
| `agent/jobagent/fonts/DejaVuSans*.ttf` (создать) | Шрифт с кириллицей для EN и RU |
| `agent/jobagent/package.py` (создать) | `PackageBuilder`: вызовы моделей, пересборка, удаление, сборка итога |
| `agent/jobagent/db.py` (изменить) | Таблица `packages` и методы к ней |
| `agent/jobagent/package_store.py` (создать) | Версии пакетов на диске и в базе |
| `agent/jobagent/package_bot.py` (создать) | Кнопки «Подготовить отклик» и «Переделать», фоновая сборка, отправка в Telegram |
| `agent/jobagent/bot.py` (изменить) | Кнопка в `kb_new`, подключение роутера пакетов |
| `agent/jobagent/config.py`, `main.py`, `cli.py` (изменить) | Настройки, запуск, команды `package` и `package-eval` |
| `agent/tests/test_facts.py`, `test_textchecks.py`, `test_factcheck.py`, `test_pdf.py`, `test_package.py`, `test_package_store.py`, `test_package_bot.py` (создать) | Тесты по задачам |
| `agent/tests/fixtures/packages/*.json` (создать) | Вакансии для eval пакетов |

---

### Задача 0: Подготовка ветки

- [ ] **Step 1:** влить `main` в ветку реализации (в `main` к этому моменту должны быть ветки `bot-tests`, `eval-harness`, `output-validators`; если какой-то нет, спросить Александра, ждать ли её). Конфликтов с MVP-2 быть не должно: MVP-2 только добавляет файлы и кнопку.
- [ ] **Step 2:** `cd agent && uv run pytest -q`, ожидаемо: всё зелёное. В задаче 8 переиспользовать `FakeSession` из `tests/test_bot.py`, в задаче 6 — `make_client` из `tests/test_scoring_wire.py`.
- [ ] **Step 3:** `uv add reportlab`, положить `DejaVuSans.ttf`, `DejaVuSans-Bold.ttf`, `DejaVuSans-Oblique.ttf` из официального релиза dejavu-fonts 2.37 в `agent/jobagent/fonts/` вместе с файлом лицензии. Коммит «MVP-2: зависимость reportlab и шрифт DejaVu».

---

### Задача 1: Номера фактов и парсер файла опыта

**Files:**
- Modify: `experience/Bachinin_Alexander_Experience.md`
- Create: `agent/jobagent/facts.py`
- Test: `agent/tests/test_facts.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Fact: id: str; employer: str; text: str; numbers: frozenset[float]; terms: frozenset[str]`
  - `@dataclass(frozen=True) class Employer: code: str; heading: str; titles: tuple[str, ...]; context_fact: str | None` (`context_fact` — номер описания места работы, `CODE-0`)
  - `@dataclass(frozen=True) class FactBase: facts: dict[str, Fact]; employers: dict[str, Employer]; headlines: tuple[str, ...]; vocabulary: frozenset[str]; not_counted: tuple[str, ...]`
  - `parse_facts(path: Path) -> FactBase`, ошибки разметки → `FactFileError(ValueError)`
  - `extract_numbers(text: str) -> frozenset[float]`
  - `extract_terms(text: str, vocabulary: frozenset[str]) -> frozenset[str]` (названия из словаря, без учёта регистра)
  - `unknown_terms(text: str, vocabulary: frozenset[str]) -> frozenset[str]` (латинские слова с заглавной не в начале предложения, которых нет в словаре и в `GENERIC_TERMS`)
  - `GENERIC_TERMS: frozenset[str]` (AI, SaaS, B2B, B2C, LLM, UX, UI, PM, MVP, API, iOS, Android, MAU, MRR, LTV, ARPU, NPS, ROI, GTM, JTBD, CustDev, PRD, QA, SEO, A/B и т.п.: не требуют совпадения с фактом)

**Разметка.** Коды: `PROF` (Профиль), `VIBE`, `CK`, `RLY`, `VERDA`, `GPTS`, `AIC` (AI-контент), `CLOWE`, `MPS`, `SKK` (Sidekick), `STR` (Streasy), `AEZ`, `MDG`, `MIR`, `SKILL` (Навыки). Описание места работы (абзац под заголовком) получает `CODE-0`, пункты `CODE-1…`. Номер в конце строки: `… 1 000+ MAU. [MPS-1]`. Раздел «Перерыв» и «Не засчитывать» номеров не получают. Номер не переиспользуется после удаления факта.

**Числа.** `1,000+` и `1 000+` → 1000; `2.5%` и `2,5%` → 2.5; `$18K` и `18K $` → 18000; `$4,5K` → 4500; `1M+` → 1000000; `4x`, `×4`, `в 4 раза`, `4 times` → 4; английские слова one…twelve → 1…12. Запятая с ровно тремя цифрами после неё — разделитель тысяч, иначе десятичная. Годы 1990–2030 из чисел исключаются (даты проверяются по месту работы, не по пункту).

**Должности.** Текст после `Должность:` / `Должности:` до первой точки, разбит по `, `. Скобки с кириллицей отбрасываются, скобки без кириллицы остаются частью должности.

**Позиционирования.** Пункт «Позиционирования, под которые уже делал резюме: …» раздела «Профиль», разбит по `, `, минус всё, что встречается в «Не засчитывать» (CPO).

- [ ] **Step 1: падающие тесты**

```python
def test_every_bullet_in_profile_and_experience_has_unique_id(): ...
    # parse_facts на настоящем файле: ни одного пункта без [CODE-N] в «Профиль», «Опыт по времени», «Навыки и инструменты»; номера уникальны
def test_fact_belongs_to_employer():
    fb = parse_facts(EXPERIENCE)
    assert fb.facts["MPS-1"].employer == "MPS" and "1 000+" in fb.facts["MPS-1"].text
    assert fb.employers["VIBE"].titles == ("Product Manager (UX & AI Development)",)
    assert fb.employers["RLY"].titles == ("Head of Content Production", "Growth Product Manager")
def test_headlines_exclude_cpo():
    fb = parse_facts(EXPERIENCE)
    assert "AI PM / AI Product Builder" in fb.headlines or "AI Product Builder" in fb.headlines
    assert not any("CPO" in h for h in fb.headlines)
def test_not_counted_section_parsed():
    assert any("хакатон" in x for x in parse_facts(EXPERIENCE).not_counted)
@pytest.mark.parametrize("text,expected", [
    ("1,000+ MAU", {1000}), ("1 000+ MAU", {1000}), ("from 1.5% to 2.5%", {1.5, 2.5}),
    ("с 1,5% до 2,5%", {1.5, 2.5}), ("$18K MRR", {18000}), ("18K $", {18000}), ("$4,5K", {4500}),
    ("1M+ views", {1_000_000}), ("в 4 раза", {4}), ("4x traffic", {4}), ("three pipelines", {3}),
    ("Feb 2026 – Oct 2026", set()),
])
def test_extract_numbers(text, expected): assert extract_numbers(text) == expected
def test_duplicate_id_raises(tmp_path): ...  # FactFileError с номером в сообщении
def test_unknown_terms_skip_sentence_start_and_generic():
    vocab = frozenset({"metabase", "ga4"})
    assert unknown_terms("Built SaaS analytics in Metabase and Kubernetes", vocab) == {"Kubernetes"}
```

- [ ] **Step 2:** `uv run pytest tests/test_facts.py -v`, ожидаемо FAIL (нет модуля).
- [ ] **Step 3:** реализовать `facts.py` по Interfaces. Словарь `vocabulary`: все латинские токены длиной ≥ 2 из файла, которые начинаются с заглавной или содержат цифру, в нижнем регистре, минус `GENERIC_TERMS`.
- [ ] **Step 4:** разметить файл опыта номерами (черновик). Тесты зелёные.
- [ ] **Step 5: eval до и после.** Файл опыта читает Sonnet в MVP-1, номера меняют промпт. `uv run python -m jobagent.cli eval scoring` на коммите до разметки и после, `--compare`. Ожидаемо: ни одной смены вердикта для вакансий с оценкой 4–5. Цифры в сообщение коммита.
- [ ] **Step 6: показать Александру** дифф файла опыта (только добавленные номера) и дождаться «ок». Без его ответа не коммитить.
- [ ] **Step 7:** `uv run pytest -q`, коммит «Факты с номерами в файле опыта и парсер facts.py» с цифрами eval.

---

### Задача 2: Структуры ответов и правила пакета

**Files:**
- Create: `agent/jobagent/package_models.py`, `agent/jobagent/package_rules.py`
- Test: `agent/tests/test_factcheck.py` (раздел правил)

**Interfaces:**
- Produces (`package_models.py`):
  - `RoleType = Literal["builder", "growth", "product"]`, `Lang = Literal["en", "ru"]`
  - `Requirement(BaseModel): text: str; facts: list[str]; status: Literal["closed", "partial", "gap"]`
  - `RequirementMap(BaseModel): role_type: RoleType; requirements: list[Requirement]`
  - `Claim(BaseModel): text: str; facts: list[str]; requirement: str | None = None` (`requirement` — текст требования из карты, которое закрывает утверждение)
  - `Job(BaseModel): employer: str; title: str; bullets: list[Claim]`
  - `ResumeDraft(BaseModel): headline: str; summary: list[Claim]; jobs: list[Job]; skills: list[Claim]`
  - `LetterSentence(Claim): paragraph: int`
  - `LetterDraft(BaseModel): subject: str | None; salutation: str; sentences: list[LetterSentence]; closing: str` (утверждения без `facts` — хук о компании и призыв; подпись добавляет код)
- Produces (`package_rules.py`):
  - `COMPOSITION: dict[RoleType, tuple[str, ...]]` — упорядоченные коды мест работы:
    - `builder`: `("VIBE", "GPTS", "MPS", "SKK", "STR", "AEZ")`
    - `growth`: `("RLY", "VIBE", "GPTS", "VERDA", "AIC", "MPS", "SKK", "STR", "AEZ")`
    - `product`: `("VIBE", "GPTS", "MPS", "SKK", "STR", "AEZ")`
  - `REQUIRED: dict[RoleType, frozenset[str]]`: всем `AEZ`; `builder` ещё `VIBE`
  - `NEVER: frozenset[str] = {"CLOWE", "MDG", "MIR"}`
  - `NESTING: dict[str, frozenset[str]]`: `{"VIBE": {"CK"}}` только для `growth` (пункт под i-free может ссылаться на `CK-…`); функция `allowed_fact_employers(job_code: str, role_type: RoleType) -> frozenset[str]`
  - `EMPLOYER_NAMES: dict[str, str]`: как место работы называется в резюме (`VIBE` → «i-free», `GPTS` → «GPT Scrambler», `STR` → «Streasy (Proofix)»)
  - `bullet_limits(position: int) -> tuple[int, int]`: `(3, 4)` для позиций 0–2, `(1, 2)` дальше
  - `CUT_ORDER: tuple[str, ...]` — шаги подгонки: `("trim:SKK", "trim:STR", "trim_long", "drop:SKK")`
  - `FORBIDDEN: tuple[tuple[str, re.Pattern], ...]` — по одному шаблону на каждый пункт «Не засчитывать», ловит сочетание, а не слово: `hackathon|хакатон` (только с VIBE), `−?30%` и `−?50%` в пунктах VIBE, `ML.{0,20}(cluster|segment|кластер|сегмент)`, `ARPU` в пунктах MPS, `time-to-market` в SKK, `brand awareness|узнаваемост` в STR, `MRR` с числом в пунктах GPTS, `\bCPO\b`, `Head of Data|KYB|compliance`, `Python` без `Claude Code` рядом, `(руководил|led).{0,30}(отдел|growth team)` в STR, `×3,1|×4,9|×7,6`
  - `STOP_PHRASES: dict[Lang, tuple[str, ...]]` — фразы таблицы «Avoid» гайда (EN) и их русские аналоги («Кому это может касаться», «командный игрок», «я идеально подхожу», «я увлечён», «эта позиция поможет мне вырасти», «я думаю, что подойду»)

- [ ] **Step 1: падающие тесты**

```python
def test_forbidden_has_pattern_for_every_not_counted_item():
    assert len(FORBIDDEN) >= len(parse_facts(EXPERIENCE).not_counted)
@pytest.mark.parametrize("text", ["Ran AI hackathons on VibeCamp", "ML-driven behavioral segmentation",
                                  "Chief Product Officer (CPO)", "Strong Python for data processing"])
def test_forbidden_catches(text): assert any(p.search(text) for _, p in FORBIDDEN)
@pytest.mark.parametrize("text", ["MRR stable within 3 months", "Python read and reviewed, written with Claude Code"])
def test_forbidden_does_not_catch_allowed(text): assert not any(p.search(text) for _, p in FORBIDDEN)
def test_composition_rules():
    assert "CK" not in COMPOSITION["builder"] and "VIBE" in REQUIRED["builder"]
    assert all("AEZ" in REQUIRED[t] for t in COMPOSITION)
    assert not NEVER & set().union(*COMPOSITION.values())
    assert allowed_fact_employers("VIBE", "growth") == {"VIBE", "CK"}
    assert allowed_fact_employers("VIBE", "builder") == {"VIBE"}
    assert bullet_limits(0) == (3, 4) and bullet_limits(3) == (1, 2)
```

Шаблоны, привязанные к месту работы (`ARPU` только в MPS), хранить как `(employer_code | None, pattern)`; тест `test_forbidden_catches` передаёт подходящее место работы.

- [ ] **Step 2:** FAIL. **Step 3:** реализовать. **Step 4:** PASS. **Step 5:** коммит «MVP-2: структуры пакета и правила состава».

---

### Задача 3: Механические проверки текста

**Files:**
- Create: `agent/jobagent/textchecks.py`
- Test: `agent/tests/test_textchecks.py`

**Interfaces:**
- Produces:
  - `find_dashes(text: str) -> list[int]` (позиции `—` и `–`)
  - `replace_dashes(text: str) -> str`: ` — ` и ` – ` между словами → `, `; тире между числами (`3–4`) → `-`; тире в начале строки → удалить. Дефис `-` не трогать.
  - `word_count(text: str) -> int` (слова по `\w+([-']\w+)*`, Unicode)
  - `find_stop_phrases(text: str, lang: Lang) -> list[str]` (без учёта регистра)
  - `detect_language(text: str) -> Lang` (`ru`, если доля кириллических букв среди всех букв ≥ 0,3)
  - `vacancy_language(detail: dict) -> Lang` — правило из Global Constraints
  - `pdf_filename(company: str | None, role: str) -> str`

- [ ] **Step 1: падающие тесты**

```python
def test_dashes_found_and_replaced():
    assert find_dashes("Hi — there – ok") == [3, 11]
    assert replace_dashes("Grew MRR — fast") == "Grew MRR, fast"
    assert replace_dashes("3–4 bullets, sign-up") == "3-4 bullets, sign-up"
    assert find_dashes(replace_dashes("A — b – c")) == []
def test_word_count(): assert word_count("Sign-up conversion grew 25%, I'm happy.") == 5
def test_stop_phrases(): assert find_stop_phrases("I am a TEAM PLAYER.", "en") == ["team player"]
def test_detect_language():
    assert detect_language("Ищем продакт-менеджера в SaaS") == "ru"
    assert detect_language("We need a PM, русский язык желателен") == "en"
def test_vacancy_language_rules():
    assert vacancy_language({"vacancy_language": "ru"}) == "ru"
    assert vacancy_language({"vacancy_language": "es"}) == "en"
    assert vacancy_language({"description": "Ищем менеджера продукта"}) == "ru"
def test_pdf_filename_masked_and_special():
    assert pdf_filename("Acme & Co / R&D", "Senior PM (AI)") == "Bachinin_Alexander_Acme_Co_R_D_Senior_PM_AI.pdf"
    assert pdf_filename(None, "Product Manager") == "Bachinin_Alexander_Product_Manager.pdf"
    assert pdf_filename("Рога и копыта", "Продакт") == "Bachinin_Alexander_Roga_i_kopyta_Prodakt.pdf"
```

Транслитерацию кириллицы сделать простой таблицей ГОСТ-подобных замен, без новой зависимости.

- [ ] **Step 2:** FAIL. **Step 3:** реализовать. **Step 4:** PASS. **Step 5:** коммит «MVP-2: механические проверки письма и имени файла».

---

### Задача 4: Проверка фактов кодом

**Files:**
- Create: `agent/jobagent/factcheck.py`
- Test: `agent/tests/test_factcheck.py`

**Interfaces:**
- Consumes: `FactBase`, `extract_numbers`, `extract_terms`, `unknown_terms` (задача 1); модели и правила (задача 2); `find_dashes`, `word_count`, `find_stop_phrases`, `detect_language` (задача 3).
- Produces:
  - `ViolationCode = Literal["unknown_fact", "wrong_employer", "no_facts", "number", "term", "unknown_term", "forbidden", "gap", "composition", "bullets", "headline", "title", "dash", "length", "stop_phrase", "language", "semantic"]`
  - `@dataclass(frozen=True) class Violation: path: str; code: ViolationCode; detail: str` — `path` вида `jobs[2].bullets[1]`, `summary[0]`, `letter[4]`, `letter.subject`, `resume`; `detail` содержит только номера фактов, числа и названия, без цитаты пункта
  - `check_claim(claim: Claim, path: str, allowed_employers: frozenset[str] | None, fb: FactBase, req_map: RequirementMap) -> list[Violation]`
  - `check_resume(draft: ResumeDraft, req_map: RequirementMap, fb: FactBase, lang: Lang) -> list[Violation]`
  - `check_letter(draft: LetterDraft, req_map: RequirementMap, fb: FactBase, lang: Lang, company: str | None, role: str) -> list[Violation]`

**Правила `check_claim`:**
1. Номер не существует → `unknown_fact`.
2. Номер не из `allowed_employers` (для summary и письма `None` = любой, кроме `NEVER`) → `wrong_employer`. Навыки — только `SKILL-…` и `PROF-…`.
3. Утверждение резюме без номеров → `no_facts` (в письме без номеров допустимо, его проверяет только проверка смысла).
4. `extract_numbers(claim) ⊄ ⋃ numbers(facts)` → `number`, `detail` = лишние числа.
5. `extract_terms(claim, vocab) ⊄ ⋃ terms(facts)` → `term`; `unknown_terms(claim)` непусто → `unknown_term`.
6. Совпадение с `FORBIDDEN` (с учётом места работы) → `forbidden`.
7. `claim.requirement` есть в карте со статусом `gap` → `gap`.

**`check_resume` дополнительно:** места работы ⊆ `COMPOSITION[role_type]`, все `REQUIRED` на месте, порядок совпадает с `COMPOSITION` → иначе `composition`; число пунктов по `bullet_limits` → `bullets`; `headline ∈ fb.headlines` → иначе `headline`; `title ∈ fb.employers[code].titles` → иначе `title`; язык каждого текста → `language`.
**`check_letter` дополнительно:** тире в теме, обращении, утверждениях, финале → `dash`; `word_count` всего письма > 300 → `length`; стоп-фразы → `stop_phrase`; язык → `language`; название компании (если не скрыта) и роль встречаются в письме дословно → иначе `title`.

- [ ] **Step 1: падающие тесты** (фикстура `fb` — настоящий файл опыта, `req_map` с одним `gap` «Strong Python»)

```python
def test_true_bullets_from_manual_resume_pass(fb, req_map):
    # Review Focus 2: правдивые пункты ручного резюме AI Product Builder, сверенные с файлом опыта
    ok = [("MPS", "Interviewed sellers, found an unmet need and launched a new analytics tool that reached 1,000+ MAU.", ["MPS-1"]),
          ("AEZ", "Built the product from zero to $18K MRR (6.6% average monthly growth); raised paid conversion from 1.5% to 2.5%.", ["AEZ-1", "AEZ-2"]),
          ("GPTS", "Lifecycle emails lifted first renewal from 43% to 57% and long-term retention from 70% to 76%.", ["GPTS-7"])]
    for code, text, facts in ok:
        assert check_claim(Claim(text=text, facts=facts), "b", frozenset({code}), fb, req_map) == []
def test_invented_number(fb, req_map):
    v = check_claim(Claim(text="Raised registration conversion by 35%", facts=["MPS-2"]), "b", frozenset({"MPS"}), fb, req_map)
    assert [x.code for x in v] == ["number"] and "35" in v[0].detail
def test_number_from_other_product(fb, req_map):
    v = check_claim(Claim(text="81% activation", facts=["GPTS-2"]), "b", frozenset({"MPS"}), fb, req_map)
    assert "wrong_employer" in {x.code for x in v}
def test_invented_market_intelligence(fb, req_map):
    v = check_claim(Claim(text="Market intelligence for Wildberries and Ozon with low-code tools", facts=["MPS-0"]), "b", frozenset({"MPS"}), fb, req_map)
    assert {"term", "unknown_term"} & {x.code for x in v}
def test_masked_gap(fb, req_map):
    c = Claim(text="Hands-on with data workflows", facts=["VIBE-1"], requirement="Strong Python")
    assert "gap" in {x.code for x in check_claim(c, "b", frozenset({"VIBE"}), fb, req_map)}
def test_forbidden_claim(fb, req_map):
    c = Claim(text="Ran AI hackathons for employees", facts=["VIBE-1"])
    assert "forbidden" in {x.code for x in check_claim(c, "b", frozenset({"VIBE"}), fb, req_map)}
def test_resume_composition(fb, req_map): ...  # builder без VIBE → composition; CK в builder → composition; заголовок "CPO" → headline; 5 пунктов на позиции 0 → bullets
def test_letter_mechanics(fb, req_map): ...   # тире в subject → dash; 320 слов → length; "team player" → stop_phrase; русское письмо при lang="en" → language
def test_violation_detail_has_no_claim_text(fb, req_map): ...  # detail не содержит слов пункта, кроме чисел и названий
```

Номера фактов в тестах подставить по реальной разметке задачи 1.

- [ ] **Step 2:** FAIL. **Step 3:** реализовать. **Step 4:** PASS. Если `test_true_bullets_from_manual_resume_pass` падает на нормализации — чинить извлечение чисел и названий, а не ослаблять проверку. **Step 5:** коммит «MVP-2: проверка фактов кодом».

---

### Задача 5: PDF и подгонка под страницу

**Files:**
- Create: `agent/jobagent/pdf.py`
- Test: `agent/tests/test_pdf.py`

**Interfaces:**
- Consumes: `ResumeDraft`, `FactBase`, `EMPLOYER_NAMES`, `CUT_ORDER`, `REQUIRED`, `pdf_filename`.
- Produces:
  - `@dataclass(frozen=True) class Contacts: email: str; linkedin: str; telegram: str; location_line: str` (`location_line` — «UTC+3 · Remote · Open to relocation · English C1 · Russian native · Spanish B1» или русская версия)
  - `STYLES: dict[str, ParagraphStyle]` — стили из `applications_2026-10-01/build_cvs.py` (размеры шрифта как там), шрифт DejaVu Sans, `leading = max(исходный, ceil(1.25 × fontSize, 0.1))`
  - `render_pdf(draft: ResumeDraft, fb: FactBase, contacts: Contacts, lang: Lang) -> tuple[bytes, int]` (PDF и число страниц; даты и описание места работы из `fb`)
  - `fit_one_page(draft: ResumeDraft, fb: FactBase, contacts: Contacts, lang: Lang, role_type: RoleType) -> tuple[ResumeDraft, bytes, list[str]]` — применяет `CUT_ORDER` по шагам, возвращает подогнанный черновик, PDF и список сделанных шагов; не убирает `REQUIRED`; если после всех шагов страниц > 1 → `PackageError("не помещается на страницу")`
  - `to_markdown(draft: ResumeDraft, fb: FactBase, contacts: Contacts) -> str` (для хранения; номера фактов в HTML-комментариях `<!-- MPS-1 -->`)
  - `class PackageError(RuntimeError)` (определить здесь, переиспользуется в задачах 6–8)

Шаги `CUT_ORDER`: `trim:SKK` / `trim:STR` — оставить первый пункт; `trim_long` — у мест с 4 пунктами убрать последний, по одному месту за шаг, начиная с конца состава; `drop:SKK` — убрать место работы.

- [ ] **Step 1: падающие тесты**

```python
def test_leading_invariant():
    assert all(s.leading >= 1.25 * s.fontSize for s in STYLES.values())
def test_short_resume_one_page(fb): assert render_pdf(SHORT_DRAFT, fb, CONTACTS, "en")[1] == 1
def test_long_resume_is_cut_not_squeezed(fb):
    draft, pdf, steps = fit_one_page(LONG_DRAFT, fb, CONTACTS, "en", "product")
    assert pdf_pages(pdf) == 1 and steps[0] == "trim:SKK"
    assert "AEZ" in [j.employer for j in draft.jobs]
def test_growth_resume_with_minimum_bullets_fits(fb): ...  # 9 мест, 3/1 пункта, пункты реальной длины
def test_impossible_resume_raises(fb): ...  # 6 мест по 4 пункта по 400 символов → PackageError
def test_russian_resume_renders_cyrillic(fb):
    pdf, pages = render_pdf(SHORT_DRAFT_RU, fb, CONTACTS_RU, "ru")
    assert pages == 1 and b"DejaVuSans" in pdf
def test_markdown_keeps_fact_ids(fb): assert "<!-- MPS-1 -->" in to_markdown(SHORT_DRAFT, fb, CONTACTS)
```

`SHORT_DRAFT`, `SHORT_DRAFT_RU`, `LONG_DRAFT` собрать в `tests/test_pdf.py` из реальных номеров фактов; `pdf_pages(pdf: bytes) -> int` — помощник теста, `len(re.findall(rb"/Type\s*/Page[^s]", pdf))`. Если `test_growth_resume_with_minimum_bullets_fits` не проходит на реалистичных пунктах, **остановиться и спросить Александра**, какое место работы убирать из growth-резюме: в спецификации этого нет.

- [ ] **Step 2:** FAIL. **Step 3:** реализовать. **Step 4:** PASS. **Step 5:** собрать PDF из `SHORT_DRAFT` в `/tmp`, открыть, глазами проверить, что строки не наезжают и вид как у ручных резюме. **Step 6:** коммит «MVP-2: генератор PDF с подгонкой под страницу».

---

### Задача 6: Сборщик пакета (модели)

**Files:**
- Create: `agent/jobagent/package.py`
- Modify: `agent/jobagent/config.py`
- Test: `agent/tests/test_package.py`

**Interfaces:**
- Consumes: всё из задач 1–5; `Usage`, `vacancy_for_prompt`, `CACHE_1H` из `scoring.py`; `Vacancy` из `db.py`.
- Produces:
  - `config.Settings`: `package_model: str = "claude-opus-5-5"` (`PACKAGE_MODEL`), `package_check_model: str = "claude-haiku-5-5"` (`PACKAGE_CHECK_MODEL`), `packages_dir: Path = AGENT_DIR / "data" / "packages"` (`PACKAGES_DIR`), `candidate_email`, `candidate_linkedin`, `candidate_telegram` (`CANDIDATE_EMAIL`, `CANDIDATE_LINKEDIN`, `CANDIDATE_TELEGRAM`)
  - `@dataclass class BuiltPackage: lang: Lang; role_type: RoleType; req_map: RequirementMap; resume: ResumeDraft; letter: LetterDraft; letter_text: str; pdf: bytes; filename: str; removed: list[Violation]; warnings: list[str]; cut_steps: list[str]`
  - `class PackageBuilder.__init__(self, knowledge_dir: Path, map_model: str, gen_model: str, check_model: str, contacts: Contacts, client: anthropic.AsyncAnthropic | None = None)`
  - `async def build(self, v: Vacancy, feedback: str | None = None, previous: BuiltPackage | None = None) -> tuple[BuiltPackage, list[tuple[str, Usage]]]` — список `(step, usage)` для событий `llm`, шаги `package_map`, `package_resume`, `package_letter`, `package_check`
  - `render_letter(draft: LetterDraft, contacts: Contacts, lang: Lang) -> str` — тема (`Subject: …` / `Тема: …`), обращение, абзацы по `paragraph`, финал, подпись «Best regards,» / «С уважением,» + имя + email | LinkedIn | Telegram

**Порядок `build`:**
1. `lang = vacancy_language(v.detail)`; карта требований — `map_model`, вход: вакансия в `<vacancy>` (как в MVP-1), `must_haves` и `missing_must_haves` из `v.assessment`, факты с номерами. Требования из `missing_must_haves` обязаны получить `gap`. Код проверяет номера; несуществующие убираются из карты.
2. Резюме — `gen_model`, системный промпт: правила состава для `role_type`, лимиты пунктов, язык, «каждое утверждение — со ссылками на номера», «Не засчитывать», вакансия как данные. Кешируемый блок: файл опыта + правила (как `scoring_system`).
3. Письмо — `gen_model`, промпт: `cover_letter_guide.md` целиком, тот же кешируемый блок, итоговое резюме (чтобы письмо не противоречило ему), обращение по имени из `detail.contact`, если оно есть.
4. Проверка кодом (`check_resume`, `check_letter`) и проверка смысла — один вызов `check_model` на весь пакет: для каждого утверждения с номерами вопрос «следует ли текст из этих фактов, без усиления роли и результата»; для утверждений письма без номеров «нет ли здесь утверждения об опыте кандидата». Ответ: список `{index, ok, reason_code}`; `ok=false` → `Violation(code="semantic")`.
5. Если нарушения есть: одна пересборка резюме и/или письма с перечнем нарушений (`path`, `code`, `detail`) в сообщении пользователя. Снова шаг 4.
6. Оставшиеся нарушения: утверждение удаляется (пункт, фраза summary, навык, предложение письма) и попадает в `removed`. `dash` — `replace_dashes` и строка в `warnings`; `length`, `stop_phrase` — строка «⚠️ проверь: …» в `warnings`; `composition` / `headline` после пересборки → `PackageError`.
7. `fit_one_page`, `pdf_filename(company, role)`.
8. `feedback` и `previous`: правка идёт в сообщение пользователя для шагов 2–3 вместе с прошлыми черновиками («вот прошлая версия, вот правка Александра»). Карта требований берётся из `previous` без повторного вызова. Проверки те же.

- [ ] **Step 1: падающие тесты** (клиент — `make_client` из `test_scoring_wire.py`, расширенный очередью ответов по одному на вызов; `fb_dir` — временная `knowledge_dir` с копиями файла опыта и `cover_letter_guide.md`)

```python
async def test_inflated_role_removed_by_semantic_check(fb_dir): ...
    # check-модель дважды отвечает ok=false для "Developed the part…" при факте «специфицировал» → removed[0].code == "semantic"
async def test_feedback_cannot_add_missing_experience(fb_dir): ...
    # правка «добавь Kubernetes», resume дважды с пунктом про Kubernetes → пункт удалён, removed[0].code == "unknown_term"
async def test_masked_company_salutation(fb_dir): ...  # company_masked=True, контакта нет → обращение без имени компании, filename без компании
async def test_build_happy_path_calls_steps_in_order(fb_dir): ...
    # 4 вызова: map(sonnet) → resume(opus) → letter(opus) → check(haiku); usage-шаги в том же порядке
async def test_invented_bullet_is_retried_then_removed(fb_dir):
    # ответ resume с пунктом "+35%" дважды → пункт удалён, removed[0].code == "number", всего 2 вызова resume
async def test_dash_in_letter_replaced_with_warning(fb_dir): ...
async def test_missing_must_have_becomes_gap(fb_dir): ...  # missing_must_haves=["Strong Python"], карта модели говорит "closed" → в итоге "gap"
async def test_feedback_reuses_map_and_passes_previous(fb_dir): ...  # 3 вызова без map; в запросе resume есть текст правки и прошлый черновик
async def test_vacancy_text_is_data_not_instructions(fb_dir): ...  # description с "ignore previous instructions" лежит внутри <vacancy> в запросе
async def test_composition_error_after_retry_raises(fb_dir): ...  # builder без VIBE дважды → PackageError
```

- [ ] **Step 2:** FAIL. **Step 3:** реализовать. **Step 4:** PASS. **Step 5:** коммит «MVP-2: сборщик пакета».

---

### Задача 7: Хранение пакетов

**Files:**
- Modify: `agent/jobagent/db.py`
- Create: `agent/jobagent/package_store.py`
- Test: `agent/tests/test_package_store.py`

**Interfaces:**
- Consumes: `BuiltPackage`, `to_markdown`.
- Produces:
  - Таблица `packages(slug TEXT, version INTEGER, status TEXT, path TEXT, tg_message_id INTEGER, created_at TEXT, updated_at TEXT, PRIMARY KEY (slug, version))`, статусы `building` / `ready` / `failed`
  - `Database`: `create_package(slug) -> int` (следующая версия, статус `building`), `set_package(slug, version, **fields)`, `get_package(slug, version) -> dict | None`, `building_package(slug) -> int | None`, `fail_stale_packages() -> int`
  - `PackageStore(db: Database, root: Path)`: `start(slug) -> int`, `save(slug, version, built: BuiltPackage, feedback: str | None) -> Path`, `fail(slug, version, code: str)`, `load(slug, version) -> BuiltPackage`
  - Папка `root/<slug>/v<N>/`: `resume.json`, `resume.md`, `<filename>.pdf`, `letter.md`, `experience_map.md`, `experience_map.json`, `check.json`, `feedback.txt` (если есть). Права папки `0o700`, файлов `0o600`.
  - События: `package_ready(version, removed, warnings, cut_steps)`, `package_failed(version, code)`.

- [ ] **Step 1: падающие тесты**

```python
async def test_versions_increment(db, tmp_path): ...  # start → 1, start → 2
async def test_save_and_load_roundtrip(db, tmp_path): ...  # load(slug, 1) == сохранённый BuiltPackage (без pdf-байтов сравнение по полям)
async def test_files_private(db, tmp_path): ...  # stat().st_mode & 0o077 == 0
async def test_restart_fails_stale_building(db, tmp_path):
    # Review Focus 1
    v = await store.start(SLUG)
    assert await db.fail_stale_packages() == 1
    assert (await db.get_package(SLUG, v))["status"] == "failed"
    assert await db.building_package(SLUG) is None
async def test_events_have_no_text(db, tmp_path): ...  # события package_* не содержат слов из письма, резюме и feedback
```

- [ ] **Step 2:** FAIL. **Step 3:** реализовать. **Step 4:** PASS. **Step 5:** коммит «MVP-2: хранение пакетов по версиям».

---

### Задача 8: Кнопки в боте

**Files:**
- Create: `agent/jobagent/package_bot.py`
- Modify: `agent/jobagent/bot.py` (`kb_new`, `HELP`, `make_dispatcher`)
- Test: `agent/tests/test_package_bot.py`

**Interfaces:**
- Consumes: `PackageBuilder`, `PackageStore`, `Database`, `Notifier`, `kb_new`.
- Produces:
  - Кнопка `📝 Подготовить отклик`, `callback_data=f"pk:{vacancy_id}"`, в `kb_new` отдельной строкой последней. В `HELP` строка о ней.
  - `class PackageRunner(bot: Bot, db: Database, store: PackageStore, builder: PackageBuilder, owner_id: int)`: `async def request(v: Vacancy, feedback: str | None = None, base_version: int | None = None) -> str` (ответ для `cb.answer`: «Собираю пакет, это займёт пару минут» / «Уже собираю») — запускает `asyncio.create_task`, одна сборка за раз на весь сервис (`asyncio.Lock`), `pending_redo: dict[int, tuple[str, int]]` (message_id запроса правки → slug, версия)
  - `build_package_router(db: Database, runner: PackageRunner, owner_id: int) -> Router`: `pk:` → `runner.request`; `rd:<vacancy_id>:<version>` → отправить `ForceReply` «Что поправить в версии N?» и запомнить в `pending_redo`; сообщение владельца в ответ на такой запрос → `runner.request(v, feedback=text, base_version=N)`
  - `make_dispatcher(db, hirify, owner_id, package_runner: PackageRunner | None = None)` — роутер пакетов подключается, только если `package_runner` передан
  - Отправка результата ответом на карточку (`reply_to_message_id = v.tg_message_id`): документ `BufferedInputFile(pdf, filename)`, подпись `f"Резюме v{N} · {role}"` (обрезать до 1024); письмо отдельным сообщением `parse_mode=None`; сводка (`removed` как «убрал: <path> (<code>)», `warnings`, `cut_steps`) отдельным сообщением, если непуста; кнопка `✏️ Переделать` (`rd:…`) на последнем сообщении
  - Ошибка сборки: `store.fail`, сообщение «Пакет не собрался: <код>», событие `package_failed`
  - События: `package_requested(version)`, `package_redo(version, base_version, feedback_len)`; `llm` по каждому `(step, usage)` из `build`

- [ ] **Step 1: падающие тесты** (`FakeSession` и фикстура `db` из `test_bot.py`; `FakeBuilder` с заданным `BuiltPackage` или исключением)

```python
async def test_card_has_package_button(): assert any(b.callback_data == f"pk:{VID}" for row in kb_new(vac).inline_keyboard for b in row)
async def test_package_button_builds_and_sends(db): ...
    # документ с именем pdf_filename, письмо отдельным сообщением parse_mode=None, кнопка rd:VID:1, события package_requested и package_ready
async def test_second_press_while_building_says_already(db):
    # Review Focus 3: FakeBuilder ждёт Event; второе нажатие → cb.answer "Уже собираю", builder.build вызван один раз
async def test_redo_uses_requested_version(db):
    # Review Focus 3: пакеты v1, v2; ответ на запрос правки к v1 → build(previous=load(v1)), новая версия 3
async def test_redo_feedback_not_logged(db, caplog):
    # Review Focus 4: правка "Напиши Анне, что жду 5000 USD" → нет в caplog.text и в events.data; есть feedback_len
async def test_stranger_cannot_build_or_redo(db): ...
async def test_build_error_reports_code(db): ...  # PackageError → сообщение и package_failed, статус failed
async def test_caption_and_texts_within_telegram_limits(db): ...
async def test_vacancy_without_detail_is_not_built(db): ...  # detail=None → сообщение «нет полного текста вакансии», builder не вызван
```

- [ ] **Step 2:** FAIL. **Step 3:** реализовать. **Step 4:** PASS. **Step 5:** `uv run pytest -q`. **Step 6:** коммит «MVP-2: кнопки «Подготовить отклик» и «Переделать»».

---

### Задача 9: Запуск, команды CLI, eval пакетов, документация

**Files:**
- Modify: `agent/jobagent/main.py`, `agent/jobagent/cli.py`, `agent/README.md`, `HOW_IT_WORKS.md`, `CLAUDE.md`, `CHANGELOG.md`
- Create: `agent/jobagent/package_eval.py`, `agent/tests/fixtures/packages/*.json`
- Test: `agent/tests/test_package.py` (раздел eval)

**Interfaces:**
- Consumes: `PackageBuilder`, `PackageStore`, `PackageRunner`, `make_dispatcher`.
- Produces:
  - `main.py`: при старте `db.fail_stale_packages()`; `PackageBuilder` и `PackageRunner` создаются, если заданы `CANDIDATE_EMAIL` и `CANDIDATE_LINKEDIN`, иначе кнопка не показывается и в журнал пишется одно предупреждение
  - `cli package --vacancy-id N [--file detail.json] [--out DIR]` — собрать пакет одной вакансии без базы и Telegram, файлы в `DIR`, сводку в консоль (для приёмки и отладки)
  - `cli package-eval [--fixtures DIR]` → `package_eval.run(fixtures: Path, builder: PackageBuilder) -> dict` и отчёт: по каждой вакансии число удалённых утверждений по кодам, предупреждения, шаги подгонки, страницы, слова письма, стоимость; итог: «утверждений без источника в финале: 0», «тире: 0», «страниц > 1: 0». Отчёт сохраняется рядом с отчётами `eval`.
  - `/stats`: строка «Пакеты: N собрано, M переделок, средняя стоимость $X» по событиям `package_ready`, `package_redo` и `llm` с шагами `package_*` (тест в `test_service.py` рядом с `test_stats_render`)
  - Фикстуры: `detail` из Hirify (`vacancies.read`, сохранить в JSON, без контактов) для вакансий ручных пакетов: AI Product Builder (1279450), QIC, TaxDome, Intch, KORPA, Maritime AI, TryHackMe, FundraiseUp — id взять из их `experience_map.md` или `README`. Плюс ловушки: вакансия с инъекцией, вакансия на русском от компании с английским названием, CPO-вакансия, builder-вакансия, требующая «strong Python», скрытая компания.

- [ ] **Step 1: падающий тест** `test_package_eval_report_counts(fb_dir)`: на двух фикстурах с фейковым клиентом отчёт содержит итоговые счётчики и не содержит текста писем.
- [ ] **Step 2:** FAIL. **Step 3:** реализовать `package_eval.py`, команды CLI, запуск в `main.py`. **Step 4:** PASS, `uv run pytest -q`.
- [ ] **Step 5:** живой прогон `uv run python -m jobagent.cli package-eval` (тратит Opus, около доллара на прогон; предупредить Александра заранее). Ожидаемо: 0 утверждений без источника в финале, 0 тире, все PDF на одну страницу. Отчёт и стоимость пакета — в сообщение коммита.
- [ ] **Step 6:** документация: `agent/README.md` (новые переменные окружения, команды), `HOW_IT_WORKS.md` (раздел «Пакет отклика»), `CLAUDE.md` (этап MVP-2 «в работе», «Стек»: генератор PDF теперь в `agent/`), `CHANGELOG.md` (запись на дату выкладки). Коммит «MVP-2: запуск, eval пакетов, документация».
- [ ] **Step 7:** ревью `git diff prod` отдельным субагентом (requesting-code-review), исправить критичное и важное. Выкладка `./deploy.sh` только с согласия Александра.
- [ ] **Step 8: приёмка Александром.** 5 пакетов на живых вакансиях. Готово, если чинить вёрстку не пришлось ни разу и в среднем не больше одной «Переделать» на пакет.

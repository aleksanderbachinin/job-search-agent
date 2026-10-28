"""Механические проверки текста письма и имени PDF."""

from jobagent.textchecks import (
    detect_language,
    find_dashes,
    find_stop_phrases,
    pdf_filename,
    replace_dashes,
    vacancy_language,
    word_count,
)


def test_dashes_found_and_replaced():
    assert find_dashes("Hi — there – ok") == [3, 11]
    assert replace_dashes("Grew MRR — fast") == "Grew MRR, fast"
    assert replace_dashes("3–4 bullets, sign-up") == "3-4 bullets, sign-up"
    assert find_dashes(replace_dashes("A — b – c")) == []


def test_replace_dashes_line_start_and_edges():
    assert replace_dashes("— Привет\nтекст") == "Привет\nтекст"
    assert replace_dashes("слово—слово") == "слово, слово"
    assert replace_dashes("конец —\nдальше") == "конец\nдальше"
    assert find_dashes(replace_dashes("— a\nb — c\n3 – 4 —")) == []


def test_word_count():
    assert word_count("Sign-up conversion grew 25%, I'm happy.") == 6
    assert word_count("It’s fine") == 2
    assert word_count("") == 0


def test_stop_phrases():
    assert find_stop_phrases("I am a TEAM PLAYER.", "en") == ["team player"]
    assert find_stop_phrases("Clean text about growth.", "en") == []


def test_stop_phrases_typographic_apostrophe_and_ru():
    assert find_stop_phrases("I’m applying for the position of PM", "en") == [
        "i'm applying for the position"
    ]
    assert find_stop_phrases("Я Командный Игрок", "ru") == ["командный игрок"]
    # английская фраза не ловится русской таблицей
    assert find_stop_phrases("team player", "ru") == []


def test_detect_language():
    assert detect_language("Ищем продакт-менеджера в SaaS") == "ru"
    assert detect_language(
        "We need a Product Manager to own growth of our SaaS platform. Русский плюс."
    ) == "en"
    assert detect_language("12345 !!!") == "en"


def test_vacancy_language_rules():
    assert vacancy_language({"vacancy_language": "ru"}) == "ru"
    assert vacancy_language({"vacancy_language": "es"}) == "en"
    assert vacancy_language({"description": "Ищем менеджера продукта"}) == "ru"
    assert vacancy_language({"description": "We hire a PM"}) == "en"
    assert vacancy_language({"vacancy_language": None, "description": None}) == "en"
    assert vacancy_language({}) == "en"


def test_pdf_filename_masked_and_special():
    assert pdf_filename("Acme & Co / R&D", "Senior PM (AI)") == "Bachinin_Alexander_Acme_Co_R_D_Senior_PM_AI.pdf"
    assert pdf_filename(None, "Product Manager") == "Bachinin_Alexander_Product_Manager.pdf"
    assert pdf_filename("Рога и копыта", "Продакт") == "Bachinin_Alexander_Roga_i_kopyta_Prodakt.pdf"
    assert pdf_filename("  ", "Product Manager") == "Bachinin_Alexander_Product_Manager.pdf"

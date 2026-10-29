import logging
from dataclasses import replace

from fastapi.testclient import TestClient

from jobagent import main
from jobagent.config import AGENT_DIR, Settings

SETTINGS = Settings(
    hirify_api_key="test",
    hirify_webhook_secret="test",
    telegram_bot_token="123456:TEST",
    telegram_owner_id=1,
    knowledge_dir=AGENT_DIR.parent,
)


def healthz(monkeypatch, version_file) -> dict:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(main, "VERSION_FILE", version_file)
    # Без with: lifespan (бот, воркер, база) не запускается, проверяется только HTTP.
    return TestClient(main.create_app(SETTINGS)).get("/healthz").json()


def test_healthz_reports_deployed_commit(monkeypatch, tmp_path):
    # VERSION пишет deploy.sh: по нему видно, какой коммит работает на сервере.
    (tmp_path / "VERSION").write_text("3f2a9c1\n", encoding="utf-8")
    assert healthz(monkeypatch, tmp_path / "VERSION") == {"ok": True, "version": "3f2a9c1"}


def test_healthz_without_version_file(monkeypatch, tmp_path):
    assert healthz(monkeypatch, tmp_path / "VERSION") == {"ok": True, "version": "unknown"}


def test_package_runner_needs_candidate_contacts(monkeypatch, tmp_path, caplog):
    from jobagent.db import Database

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    db = Database(tmp_path / "t.sqlite3")
    with caplog.at_level(logging.WARNING, logger="jobagent"):
        assert main.make_package_runner(SETTINGS, None, db) is None
        assert main.make_package_runner(replace(SETTINGS, candidate_email="a@example.com"), None, db) is None
    assert len([r for r in caplog.records if "CANDIDATE_" in r.getMessage()]) == 2
    # Предупреждение без самих контактов.
    assert "a@example.com" not in caplog.text


def test_package_runner_built_from_settings(monkeypatch, tmp_path):
    from jobagent.db import Database

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    settings = replace(
        SETTINGS,
        candidate_email="a@example.com",
        candidate_linkedin="https://www.linkedin.com/in/example/",
        packages_dir=tmp_path / "packages",
    )
    runner = main.make_package_runner(settings, None, Database(tmp_path / "t.sqlite3"))
    assert runner is not None and runner.owner_id == SETTINGS.telegram_owner_id
    b = runner.builder
    assert (b.map_model, b.gen_model, b.check_model) == (
        settings.scoring_model, settings.package_model, settings.package_check_model
    )
    assert b.contacts.email == "a@example.com" and b.contacts.telegram == ""
    assert runner.store.root == tmp_path / "packages"


def test_sql_parameters_are_not_logged():
    # На DEBUG aiosqlite пишет параметры запросов, а в них текст вакансии.
    main.configure_logging()
    assert logging.getLogger("aiosqlite").getEffectiveLevel() >= logging.WARNING


def broken_knowledge(tmp_path):
    """Копия файлов знаний, где у пункта MPS-1 нет номера факта: ошибка разметки файла опыта."""
    import shutil

    from jobagent.scoring import EXPERIENCE_DIR, KNOWLEDGE_FILES, MASTER_RESUME

    k = tmp_path / "knowledge"
    for name in set(KNOWLEDGE_FILES.values()) | {"cover_letter_guide.md"}:
        src = AGENT_DIR.parent / name
        if src.exists():
            (k / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, k / name)
    experience = k / EXPERIENCE_DIR / MASTER_RESUME
    experience.parent.mkdir(parents=True, exist_ok=True)
    text = (AGENT_DIR.parent / EXPERIENCE_DIR / MASTER_RESUME).read_text(encoding="utf-8")
    assert " [MPS-1]" in text
    experience.write_text(text.replace(" [MPS-1]", "", 1), encoding="utf-8")
    return k


def test_fact_file_error_turns_packages_off(monkeypatch, tmp_path, caplog):
    # F13: ошибка разметки файла опыта выключает пакеты, а не весь сервис.
    from jobagent.db import Database

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    settings = replace(
        SETTINGS,
        knowledge_dir=broken_knowledge(tmp_path),
        candidate_email="a@example.com",
        candidate_linkedin="https://www.linkedin.com/in/example/",
        packages_dir=tmp_path / "packages",
    )
    with caplog.at_level(logging.WARNING, logger="jobagent"):
        assert main.make_package_runner(settings, None, Database(tmp_path / "t.sqlite3")) is None
    (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "FactFileError" in record.getMessage()
    # Без текста файла опыта: в сообщении исключения цитата пункта.
    assert "Провёл интервью" not in caplog.text and "MPS" not in caplog.text

    # MVP-1 работает: приложение собирается, /healthz отвечает.
    monkeypatch.setattr(main, "VERSION_FILE", tmp_path / "VERSION")
    assert TestClient(main.create_app(settings)).get("/healthz").json() == {"ok": True, "version": "unknown"}

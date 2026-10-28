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

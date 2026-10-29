from pathlib import Path

from jobagent.config import Settings


def test_packages_dir_defaults_next_to_db(monkeypatch):
    # На сервере systemd даёт писать только в папку базы: пакеты должны лежать там же.
    monkeypatch.delenv("PACKAGES_DIR", raising=False)
    monkeypatch.setenv("DB_PATH", "/opt/jobagent/data/jobagent.sqlite3")
    assert Settings.from_env().packages_dir == Path("/opt/jobagent/data/packages")


def test_packages_dir_from_env(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/opt/jobagent/data/jobagent.sqlite3")
    monkeypatch.setenv("PACKAGES_DIR", "/srv/packages")
    assert Settings.from_env().packages_dir == Path("/srv/packages")

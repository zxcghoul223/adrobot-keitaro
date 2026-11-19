from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app.config import Settings
from app.database import make_database
from app.demo import DemoClient
from app.main import create_app
from app.models import Base

ROOT = Path(__file__).resolve().parents[1]


def migration_config(database_url):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def settings_for(database_url):
    return Settings(
        _env_file=None,
        app_mode="demo",
        database_url=database_url,
        campaign_prefix="test_Unit_",
    )


@pytest.fixture(autouse=True)
def isolated_migration_settings(monkeypatch):
    # migrations/env.py must not consult the developer's live .env during tests.
    monkeypatch.setattr("app.config.Settings", lambda: settings_for("sqlite://"))


def test_upgrade_downgrade_and_reupgrade_on_a_fresh_database(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    config = migration_config(database_url)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == {
            "campaign_states",
            "operations",
            "demo_campaigns",
            "alembic_version",
        }
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
        for table in Base.metadata.sorted_tables:
            actual = {column["name"]: column for column in inspector.get_columns(table.name)}
            assert set(actual) == set(table.columns.keys())
            for column in table.columns:
                assert actual[column.name]["nullable"] is column.nullable
        constraints = inspector.get_unique_constraints("campaign_states")
        assert any(set(item["column_names"]) == {"mode", "remote_id"} for item in constraints)
        command.check(config)
        command.current(config)
        command.downgrade(config, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        command.upgrade(config, "head")
        command.check(config)
        assert "campaign_states" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_application_refuses_startup_without_migrations(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'unmigrated.db').as_posix()}"
    app = create_app(settings_for(database_url))
    with pytest.raises(RuntimeError, match="alembic upgrade head"), TestClient(app):
        pass
    engine = create_engine(database_url)
    try:
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_migrated_application_starts_and_closes_the_client(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    command.upgrade(migration_config(database_url), "head")
    auxiliary_engine, sessions = make_database(database_url)

    class ClosingDemo(DemoClient):
        closed = False

        async def close(self):
            self.closed = True

    upstream = ClosingDemo(sessions)
    app = create_app(settings_for(database_url), client=upstream)
    try:
        with TestClient(app) as client:
            assert client.get("/healthz").json() == {"status": "ok", "mode": "demo"}
            assert upstream.closed is False
        assert upstream.closed is True
    finally:
        auxiliary_engine.dispose()

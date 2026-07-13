from pathlib import Path

import pytest

from books_of_time.config import loader
from books_of_time.config.loader import load_config
from books_of_time.task_orchestrator.discovery_sources import (
    resolve_discovery_uid_sources,
)


def test_default_config_path_points_to_repo_config_directory() -> None:
    assert loader._DEFAULT_CONFIG_PATH == (
        Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    )


def test_example_config_monitors_primary_game_accounts_by_default() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml.example"

    cfg = load_config(config_path, environ={})

    assert cfg["discovery"]["matrix_uids"] == []
    assert cfg["discovery"]["game_uid_pools"] == {
        "genshin_impact": {
            "game_id": "genshin_impact",
            "official": True,
            "monitored": True,
            "uids": [401742377],
        },
        "wuthering_waves": {
            "game_id": "wuthering_waves",
            "official": True,
            "monitored": True,
            "uids": [1955897084],
        },
        "honkai_star_rail": {
            "game_id": "honkai_star_rail",
            "official": True,
            "monitored": True,
            "uids": [1340190821],
        },
        "zenless_zone_zero": {
            "game_id": "zenless_zone_zero",
            "official": True,
            "monitored": True,
            "uids": [1636034895],
        },
        "honkai_impact_3rd": {
            "game_id": "honkai_impact_3rd",
            "official": True,
            "monitored": True,
            "uids": [27534330],
        },
        "arknights_endfield": {
            "game_id": "arknights_endfield",
            "official": True,
            "monitored": True,
            "uids": [1265652806],
        },
        "arknights": {
            "game_id": "arknights",
            "official": True,
            "monitored": True,
            "uids": [161775300],
        },
    }
    assert cfg["discovery"]["event_uid_pools"] == {}


def test_discovery_source_resolution_applies_explicit_defaults() -> None:
    sources = resolve_discovery_uid_sources(
        {
            "matrix_uids": [100],
            "game_uid_pools": {"genshin": {"uids": [100]}},
            "event_uid_pools": {"launch": {"uids": [200]}},
        }
    )

    assert [source.as_payload() for source in sources] == [
        {
            "source_mid": "100",
            "pool_type": "matrix",
            "pool_id": "matrix",
            "game_id": None,
            "official": False,
            "monitored": True,
        },
        {
            "source_mid": "100",
            "pool_type": "game",
            "pool_id": "genshin",
            "game_id": "genshin",
            "official": True,
            "monitored": True,
        },
        {
            "source_mid": "200",
            "pool_type": "event",
            "pool_id": "launch",
            "game_id": None,
            "official": False,
            "monitored": True,
        },
    ]


@pytest.mark.parametrize("field", ["official", "monitored"])
def test_discovery_source_resolution_rejects_string_booleans(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        resolve_discovery_uid_sources(
            {
                "game_uid_pools": {
                    "genshin": {
                        "uids": [100],
                        field: "false",
                    }
                }
            }
        )


def test_load_config_applies_service_environment_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
database:
  url: sqlite+aiosqlite:///base.db
storage:
  raw_dir: ./base/raw
  media_dir: ./base/media
service:
  roles: [worker]
  shutdown_grace_seconds: 60
accounts:
  enabled: true
  active_account_id: default
  credentials_path: ./data/accounts/credentials.enc
  key_path: ./data/accounts/master.key
  auto_refresh: true
  refresh_check_seconds: 21600
""".lstrip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_path,
        environ={
            "BOT_DATABASE_URL": "postgresql+asyncpg://host/books",
            "BOT_RAW_DIR": "/archive/raw",
            "BOT_MEDIA_DIR": "/archive/media",
            "BOT_RAW_STORAGE_BACKEND": "minio",
            "BOT_MINIO_ENDPOINT": "minio.internal:9000",
            "BOT_MINIO_ACCESS_KEY": "access",
            "BOT_MINIO_SECRET_KEY": "secret",
            "BOT_MINIO_BUCKET": "books-raw",
            "BOT_MINIO_PREFIX": "evidence/raw",
            "BOT_MINIO_SECURE": "false",
            "BOT_MINIO_CREATE_BUCKET": "true",
            "BOT_INSTANCE_ID": "collector-a",
            "BOT_SERVICE_ROLES": "worker, scheduler,",
            "BOT_SHUTDOWN_GRACE_SECONDS": "45.5",
            "BOT_ACCOUNT_ENABLED": "false",
            "BOT_ACCOUNT_ID": "researcher",
            "BOT_ACCOUNT_CREDENTIALS_PATH": "/archive/accounts/credentials.enc",
            "BOT_ACCOUNT_KEY_PATH": "/archive/accounts/master.key",
            "BOT_ACCOUNT_AUTO_REFRESH": "true",
            "BOT_ACCOUNT_REFRESH_SECONDS": "3600",
        },
    )

    assert cfg["database"]["url"] == "postgresql+asyncpg://host/books"
    assert cfg["storage"]["raw_dir"] == "/archive/raw"
    assert cfg["storage"]["media_dir"] == "/archive/media"
    assert cfg["storage"]["backend"] == "minio"
    assert cfg["storage"]["minio"] == {
        "endpoint": "minio.internal:9000",
        "access_key": "access",
        "secret_key": "secret",
        "bucket": "books-raw",
        "prefix": "evidence/raw",
        "secure": False,
        "create_bucket": True,
    }
    assert cfg["service"]["instance_id"] == "collector-a"
    assert cfg["service"]["roles"] == ["worker", "scheduler"]
    assert cfg["service"]["shutdown_grace_seconds"] == 45.5
    assert cfg["accounts"] == {
        "enabled": False,
        "active_account_id": "researcher",
        "credentials_path": "/archive/accounts/credentials.enc",
        "key_path": "/archive/accounts/master.key",
        "auto_refresh": True,
        "refresh_check_seconds": 3600,
    }


def test_load_config_rejects_invalid_account_boolean_override(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("database: {url: 'sqlite+aiosqlite:///test.db'}\n")

    try:
        load_config(config_path, environ={"BOT_ACCOUNT_ENABLED": "sometimes"})
    except ValueError as exc:
        assert "BOT_ACCOUNT_ENABLED" in str(exc)
    else:
        raise AssertionError("Invalid boolean override should fail")


def test_explicit_config_path_takes_precedence_over_bot_config(tmp_path: Path) -> None:
    explicit_path = tmp_path / "explicit.yaml"
    explicit_path.write_text(
        "database: {url: 'sqlite+aiosqlite:///explicit.db'}\n",
        encoding="utf-8",
    )
    environment_path = tmp_path / "environment.yaml"
    environment_path.write_text(
        "database: {url: 'sqlite+aiosqlite:///environment.db'}\n",
        encoding="utf-8",
    )

    explicit = load_config(
        explicit_path,
        environ={"BOT_CONFIG": str(environment_path)},
    )
    from_environment = load_config(
        environ={"BOT_CONFIG": str(environment_path)},
    )

    assert explicit["database"]["url"].endswith("explicit.db")
    assert from_environment["database"]["url"].endswith("environment.db")

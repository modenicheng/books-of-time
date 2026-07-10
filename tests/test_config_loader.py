from pathlib import Path

from books_of_time.config import loader
from books_of_time.config.loader import load_config


def test_default_config_path_points_to_repo_config_directory() -> None:
    assert loader._DEFAULT_CONFIG_PATH == (
        Path(__file__).resolve().parents[1] / "config" / "config.yaml"
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
""".lstrip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_path,
        environ={
            "BOT_DATABASE_URL": "postgresql+asyncpg://host/books",
            "BOT_RAW_DIR": "/archive/raw",
            "BOT_MEDIA_DIR": "/archive/media",
            "BOT_INSTANCE_ID": "collector-a",
            "BOT_SERVICE_ROLES": "worker, scheduler,",
            "BOT_SHUTDOWN_GRACE_SECONDS": "45.5",
        },
    )

    assert cfg["database"]["url"] == "postgresql+asyncpg://host/books"
    assert cfg["storage"]["raw_dir"] == "/archive/raw"
    assert cfg["storage"]["media_dir"] == "/archive/media"
    assert cfg["service"]["instance_id"] == "collector-a"
    assert cfg["service"]["roles"] == ["worker", "scheduler"]
    assert cfg["service"]["shutdown_grace_seconds"] == 45.5


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

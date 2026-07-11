from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_systemd_unit_runs_service_with_health_gate_and_restart() -> None:
    unit = (ROOT / "deploy" / "systemd" / "books-of-time.service").read_text(
        encoding="utf-8"
    )

    assert "User=books-of-time" in unit
    assert "Group=books-of-time" in unit
    assert "WorkingDirectory=/opt/books-of-time" in unit
    assert "EnvironmentFile=/etc/books-of-time/books-of-time.env" in unit
    assert (
        "ExecStartPre=/opt/books-of-time/.venv/bin/python main.py service doctor"
        in unit
    )
    assert "ExecStart=/opt/books-of-time/.venv/bin/python main.py service run" in unit
    assert "Restart=on-failure" in unit
    assert "StateDirectory=books-of-time" in unit
    assert "TimeoutStopSec=70" in unit
    assert "alembic upgrade" not in unit


def test_linux_environment_example_uses_external_database_and_local_storage() -> None:
    environment = (ROOT / "deploy" / "books-of-time.env.example").read_text(
        encoding="utf-8"
    )

    assert "BOT_DATABASE_URL=postgresql+asyncpg://" in environment
    assert "BOT_RAW_DIR=/var/lib/books-of-time/raw" in environment
    assert "BOT_MEDIA_DIR=/var/lib/books-of-time/media" in environment
    assert "BOT_RAW_STORAGE_BACKEND=filesystem" in environment
    assert (
        "BOT_ACCOUNT_CREDENTIALS_PATH=/var/lib/books-of-time/accounts/credentials.enc"
        in environment
    )
    assert (
        "BOT_ACCOUNT_KEY_PATH=/var/lib/books-of-time/accounts/master.key" in environment
    )
    assert "BOT_SERVICE_ROLES=worker,scheduler" in environment
    assert "CHANGE_ME" in environment


def test_deployment_guide_covers_supported_runtime_paths() -> None:
    guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    for required in (
        "alembic upgrade head",
        "init-db --adopt-legacy",
        "host.docker.internal",
        "pg_hba.conf",
        "docker compose --env-file deploy/docker.env up -d",
        "systemctl enable --now books-of-time",
        "uv run python main.py service run",
        "service health",
        "service status",
        "备份",
        "回滚",
        "docs/LOGIN.md",
        "BOT_RAW_STORAGE_BACKEND=minio",
        "图片仍写入本地",
    ):
        assert required in guide


def test_readme_links_deployment_guide() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "[DEPLOYMENT](docs/DEPLOYMENT.md)" in readme
    assert "[LOGIN](docs/LOGIN.md)" in readme

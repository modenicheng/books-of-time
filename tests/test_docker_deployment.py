from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_runs_only_application_with_external_database() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

    assert set(compose["services"]) == {"books-of-time"}
    service = compose["services"]["books-of-time"]
    assert service["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    assert service["restart"] == "unless-stopped"
    assert service["stop_grace_period"] == "70s"
    assert "ports" not in service
    assert service["extra_hosts"] == ["host.docker.internal:host-gateway"]

    environment = service["environment"]
    assert environment["BOT_DATABASE_URL"] == "${BOT_DATABASE_URL:?required}"
    assert environment["BOT_CONFIG"] == "/app/config/config.yaml.example"
    assert environment["BOT_RAW_DIR"] == "/var/lib/books-of-time/raw"
    assert environment["BOT_MEDIA_DIR"] == "/var/lib/books-of-time/media"
    assert environment["BOT_ACCOUNT_CREDENTIALS_PATH"] == (
        "/var/lib/books-of-time/accounts/credentials.enc"
    )
    assert environment["BOT_ACCOUNT_KEY_PATH"] == (
        "/var/lib/books-of-time/accounts/master.key"
    )

    volumes = service["volumes"]
    assert "${BOT_DATA_DIR:-./data}/raw:/var/lib/books-of-time/raw" in volumes
    assert "${BOT_DATA_DIR:-./data}/media:/var/lib/books-of-time/media" in volumes
    assert "${BOT_DATA_DIR:-./data}/accounts:/var/lib/books-of-time/accounts" in volumes
    assert service["healthcheck"]["test"][-2:] == ["service", "health"]


def test_dockerfile_uses_pinned_uv_image_and_non_root_runtime() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ghcr.io/astral-sh/uv:0.8.15-python3.12-bookworm-slim" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "USER books-of-time" in dockerfile
    assert 'CMD ["/app/.venv/bin/python", "main.py", "service", "run"]' in dockerfile
    assert 'service", "health' in dockerfile
    assert "postgres:" not in dockerfile.lower()
    assert "/var/lib/books-of-time/accounts" in dockerfile


def test_docker_context_excludes_local_secrets_and_data() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    ignored = set(dockerignore.splitlines())

    assert ".venv" in ignored
    assert "data" in ignored
    assert "config/config.yaml" in ignored
    assert ".git" in ignored


def test_container_environment_example_has_no_real_credentials() -> None:
    environment = (ROOT / "deploy" / "docker.env.example").read_text(encoding="utf-8")

    assert "BOT_DATABASE_URL=postgresql+asyncpg://" in environment
    assert "password@" not in environment
    assert "BOT_DATA_DIR=" in environment
    assert "BOT_ACCOUNT_ID=default" in environment

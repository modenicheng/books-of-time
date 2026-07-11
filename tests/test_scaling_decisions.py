from pathlib import Path


def test_scaling_decisions_are_explicit_and_reversible() -> None:
    decision = (
        Path(__file__).resolve().parents[1] / "docs" / "SCALING_DECISIONS.md"
    ).read_text(encoding="utf-8")

    for component in ("TimescaleDB", "ClickHouse", "OpenSearch", "Meilisearch"):
        assert component in decision
    for required in (
        "Decision: Do not adopt now",
        "PostgreSQL remains the evidence system of record",
        "Trigger",
        "Benchmark gate",
        "Rollback",
        "raw_payload_id",
        "comment_observation_id",
        "p95",
    ):
        assert required in decision

from pathlib import Path


def test_scaling_decisions_are_explicit_and_reversible() -> None:
    decision = (
        Path(__file__).resolve().parents[1] / "docs" / "SCALING_EVALUATION.md"
    ).read_text(encoding="utf-8")

    for component in ("TimescaleDB", "ClickHouse", "OpenSearch", "Meilisearch"):
        assert component in decision
    for required in (
        "当前结论",
        "PostgreSQL 继续是事实源",
        "复评",
        "Benchmark Gate",
        "Rollback",
        "raw_payload_id",
        "comment_observation_id",
        "p95",
    ):
        assert required in decision

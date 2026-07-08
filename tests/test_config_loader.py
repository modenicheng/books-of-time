from pathlib import Path

from books_of_time.config import loader


def test_default_config_path_points_to_repo_config_directory() -> None:
    assert loader._DEFAULT_CONFIG_PATH == (
        Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    )

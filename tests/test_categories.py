from __future__ import annotations

from pathlib import Path

import pytest

from social_surveyor.config import ConfigError, load_categories


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_load_valid_categories(tmp_path: Path) -> None:
    _write(
        tmp_path / "demo" / "categories.yaml",
        """
version: 1
categories:
  - id: cost_complaint
    label: "Cost complaint"
    description: "Complaining about price."
  - id: off_topic
    label: "Off topic"
    description: "Not relevant."
urgency_scale:
  - range: [0, 5]
    meaning: "Low"
  - range: [6, 10]
    meaning: "High"
""",
    )

    cfg = load_categories("demo", projects_root=tmp_path)

    assert cfg.version == 1
    assert [c.id for c in cfg.categories] == ["cost_complaint", "off_topic"]
    assert cfg.urgency_scale[0].range == [0, 5]


def test_missing_categories_yaml_raises_readable_error(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()

    with pytest.raises(ConfigError) as exc:
        load_categories("demo", projects_root=tmp_path)

    msg = str(exc.value)
    assert "categories.yaml" in msg
    assert "setup --project demo" in msg  # points operator at the wizard


def test_category_id_must_be_snake_case(tmp_path: Path) -> None:
    _write(
        tmp_path / "demo" / "categories.yaml",
        """
version: 1
categories:
  - id: "Cost Complaint"
    label: "x"
    description: "y"
urgency_scale:
  - range: [0, 10]
    meaning: "all"
""",
    )

    with pytest.raises(ConfigError) as exc:
        load_categories("demo", projects_root=tmp_path)

    assert "categories.0.id" in str(exc.value) or "pattern" in str(exc.value)


def test_urgency_range_must_be_two_ints(tmp_path: Path) -> None:
    _write(
        tmp_path / "demo" / "categories.yaml",
        """
version: 1
categories:
  - id: cost_complaint
    label: "x"
    description: "y"
urgency_scale:
  - range: [0]
    meaning: "invalid"
""",
    )

    with pytest.raises(ConfigError) as exc:
        load_categories("demo", projects_root=tmp_path)

    assert "urgency_scale" in str(exc.value)


def test_example_and_opendata_configs_load() -> None:
    """Ensure the seeded configs in-repo are valid."""
    ex = load_categories("example")
    op = load_categories("opendata")
    assert len(ex.categories) >= 3
    assert len(op.categories) >= 3
    # Seed parity check: opendata starts identical to example.
    assert [c.id for c in ex.categories] == [c.id for c in op.categories]

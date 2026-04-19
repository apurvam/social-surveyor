from __future__ import annotations

from pathlib import Path

from social_surveyor.cli import _db_path


def test_db_path_defaults_to_relative_data_dir(monkeypatch):
    monkeypatch.delenv("SOCIAL_SURVEYOR_DATA_DIR", raising=False)
    assert _db_path("opendata") == Path("data") / "opendata.db"


def test_db_path_honors_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("SOCIAL_SURVEYOR_DATA_DIR", str(tmp_path))
    assert _db_path("opendata") == tmp_path / "opendata.db"

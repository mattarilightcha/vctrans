"""テストの共通。GPU・音の機器・ネットは使わない。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vc import config  # noqa: E402


@pytest.fixture
def root(tmp_path, monkeypatch):
    """設定の置き場（config.toml・profiles・logs・.env）を空のフォルダにする。"""
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "PROFILES", tmp_path / "profiles")
    monkeypatch.setattr(config, "ENV", tmp_path / ".env")
    for name in config.KEY_NAMES.values():
        for n in name:
            monkeypatch.delenv(n, raising=False)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path

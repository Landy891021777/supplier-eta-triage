# -*- coding: utf-8 -*-
"""
每一頁都要能在離線、沒有金鑰的情況下打開。

頁面拆成檔案後，最常見的壞法是某頁少 import 一個東西，只有點到那頁才會爆。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "views").glob("*.py")
               if p.name != "__init__.py")


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    import planner_settings
    monkeypatch.setattr(planner_settings, "DEFAULT_DB", tmp_path / "ps.db")


def test_home_page_runs():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    assert not at.exception, at.exception


@pytest.mark.parametrize("page", PAGES)
def test_every_page_runs(page):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    at.switch_page(page).run()
    assert not at.exception, (page, at.exception)

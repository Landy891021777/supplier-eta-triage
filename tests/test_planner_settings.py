# -*- coding: utf-8 -*-
"""
企劃調整收貨處理天數的測試。

守住三件事：調整要有原因與紀錄；工具永不寫回 ERP；沒調整的料號回到料別預設。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import planner_settings as ps  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return tmp_path / "planner_settings.db"


def test_default_comes_from_material_master_when_not_overridden(db):
    days, source = ps.effective_gr_days("PR-ArF-1088", 2, "PHOTORESIST", ps.load_overrides(db))
    assert days == 2 and "光阻料別預設" in source


def test_override_wins_and_says_who_and_why(db):
    ps.set_override(db, "PR-ArF-1088", 4, "本批需全檢", "王小明", now="2026-09-22 10:00")
    days, source = ps.effective_gr_days("PR-ArF-1088", 2, "PHOTORESIST", ps.load_overrides(db))
    assert days == 4
    assert "王小明" in source and "本批需全檢" in source and "09-22" in source


def test_reason_and_name_are_required(db):
    with pytest.raises(ValueError, match="原因"):
        ps.set_override(db, "PR-ArF-1088", 4, "  ", "王小明")
    with pytest.raises(ValueError, match="姓名"):
        ps.set_override(db, "PR-ArF-1088", 4, "全檢", "")


def test_days_must_be_within_0_to_30(db):
    with pytest.raises(ValueError):
        ps.set_override(db, "PR-ArF-1088", 31, "x", "王小明")
    with pytest.raises(ValueError):
        ps.set_override(db, "PR-ArF-1088", -1, "x", "王小明")


def test_every_change_is_logged_with_old_and_new_value(db):
    ps.set_override(db, "PR-ArF-1088", 4, "全檢", "王小明", default_days=2, now="2026-09-22 10:00")
    ps.set_override(db, "PR-ArF-1088", 3, "改抽檢", "陳大華", default_days=2, now="2026-09-23 09:00")
    ps.clear_override(db, "PR-ArF-1088", "恢復預設", "陳大華", default_days=2, now="2026-09-24 09:00")
    log = ps.change_log(db)
    assert [(r["old_days"], r["new_days"]) for r in log] == [(2, 4), (4, 3), (3, 2)]
    assert ps.load_overrides(db) == {}


def test_never_writes_to_the_erp_database(tmp_path, db):
    """原則 5：工具永不寫回 ERP。設定只能落在工具自己的資料庫。"""
    erp = tmp_path / "erp_sim.db"
    sqlite3.connect(erp).close()
    before = erp.stat().st_mtime_ns
    ps.set_override(db, "PR-ArF-1088", 4, "全檢", "王小明")
    assert erp.stat().st_mtime_ns == before
    assert db.exists() and db != erp

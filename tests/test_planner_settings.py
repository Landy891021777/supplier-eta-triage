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


def test_missing_default_in_material_master_is_reported_honestly(db):
    """
    I-4：料號主檔沒維護這個欄位（NaN／None）時，回 0 天不能跟「查過、
    確實是 0 天」講一樣的話——企劃看到「光罩料別預設」會以為系統真的
    查過光罩的預設值，實際上主檔根本沒有這筆資料。
    """
    days, source = ps.effective_gr_days("PR-ArF-1088", float("nan"), "PHOTORESIST", {})
    assert days == 0
    assert "未維護" in source

    days2, source2 = ps.effective_gr_days("PR-ArF-1088", None, "PHOTORESIST", {})
    assert days2 == 0
    assert "未維護" in source2


def test_missing_category_shows_unclassified_not_python_none(db):
    days, source = ps.effective_gr_days("PR-ArF-1088", 2, None, {})
    assert days == 2
    assert "未分類" in source
    assert "None" not in source

    days2, source2 = ps.effective_gr_days("PR-ArF-1088", 2, float("nan"), {})
    assert "未分類" in source2


def test_days_must_be_a_whole_number(db):
    """2.9 天被 int() 悄悄截斷成 2 天，企劃完全不會發現天數被改了。"""
    with pytest.raises(ValueError, match="整數"):
        ps.set_override(db, "PR-ArF-1088", 2.9, "x", "王小明")


def test_days_cannot_be_nan_or_none(db):
    with pytest.raises(ValueError):
        ps.set_override(db, "PR-ArF-1088", float("nan"), "x", "王小明")
    with pytest.raises(ValueError):
        ps.set_override(db, "PR-ArF-1088", None, "x", "王小明")


def test_string_integer_days_are_accepted(db):
    """允許 "4" 這種可轉整數的字串（表單輸入常見），但不允許 "4.5"。"""
    ps.set_override(db, "PR-ArF-1088", "4", "全檢", "王小明")
    days, _ = ps.effective_gr_days("PR-ArF-1088", 2, "PHOTORESIST",
                                   ps.load_overrides(db))
    assert days == 4
    with pytest.raises(ValueError, match="整數"):
        ps.set_override(db, "PR-ArF-1088", "4.5", "x", "王小明")


def test_blank_material_id_is_rejected(db):
    with pytest.raises(ValueError, match="料號"):
        ps.set_override(db, "  ", 4, "x", "王小明")
    with pytest.raises(ValueError, match="料號"):
        ps.set_override(db, "", 4, "x", "王小明")
    with pytest.raises(ValueError, match="料號"):
        ps.clear_override(db, "", "x", "王小明", default_days=2)


def test_clear_override_validates_days_the_same_way(db):
    """clear_override 恢復預設用的 default_days 也要走一樣的嚴格檢查。"""
    ps.set_override(db, "PR-ArF-1088", 4, "全檢", "王小明")
    with pytest.raises(ValueError, match="整數"):
        ps.clear_override(db, "PR-ArF-1088", "恢復預設", "王小明", default_days=2.9)

# -*- coding: utf-8 -*-
"""
供應商歷史統計與到料落差估計的測試。

估計函式用小型手造資料驗證邏輯；讀真實資料庫的測試放在檔案後半。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import supplier_stats  # noqa: E402


def _outcomes(rows: list[tuple[str, int, int]]) -> pd.DataFrame:
    """rows = [(supplier_id, delay_days, reschedule_count), ...]"""
    return pd.DataFrame(rows, columns=["supplier_id", "delay_days", "reschedule_count"])


def test_uses_only_rescheduled_orders_when_enough_samples():
    """
    生管收到的是「已經跳票、剛給新日期」的單。
    若把從沒改期、準時到的單一起算，會系統性低估這種單的風險。
    """
    df = _outcomes([("A", 0, 0)] * 30 + [("A", 10, 2)] * 25)
    est = supplier_stats.estimate_delay(df, "A", percentile=0.80)
    assert est["available"] and est["basis"] == "改期過的單"
    assert est["delay_days"] == 10 and est["n"] == 25


def test_falls_back_to_all_orders_and_says_so():
    df = _outcomes([("A", 0, 0)] * 30 + [("A", 10, 1)] * 5)
    est = supplier_stats.estimate_delay(df, "A", percentile=0.80)
    assert est["available"]
    assert est["basis"] == "全部單（改期單樣本不足）"
    assert est["n"] == 35


def test_refuses_when_samples_insufficient():
    """只有 10 筆的統計不該被當成依據，寧可說樣本不足。"""
    df = _outcomes([("A", 5, 1)] * 10)
    est = supplier_stats.estimate_delay(df, "A", percentile=0.80)
    assert est["available"] is False
    assert "樣本不足" in est["reason"]


def test_unknown_supplier_is_unavailable_not_an_error():
    est = supplier_stats.estimate_delay(_outcomes([("A", 5, 1)] * 30), "Z", percentile=0.8)
    assert est["available"] is False


def test_delay_is_never_negative():
    """供應商全都提早到，保守到料日也不能比他說的日期還早。"""
    df = _outcomes([("A", -2, 1)] * 30)
    est = supplier_stats.estimate_delay(df, "A", percentile=0.95)
    assert est["delay_days"] == 0


def test_higher_percentile_is_never_smaller():
    df = _outcomes([("A", d, 1) for d in range(0, 40)])
    lo = supplier_stats.estimate_delay(df, "A", percentile=0.80)["delay_days"]
    hi = supplier_stats.estimate_delay(df, "A", percentile=0.95)["delay_days"]
    assert hi >= lo


def test_conservative_eta_adds_delay_to_promised_date():
    df = _outcomes([("A", 9, 1)] * 30)
    r = supplier_stats.conservative_eta("A", "2026-10-29", df)
    assert r["available"]
    assert r["conservative"] == "2026-11-07"
    assert r["conservative"] >= r["promised"]


def test_conservative_eta_rejects_unparseable_date():
    df = _outcomes([("A", 9, 1)] * 30)
    r = supplier_stats.conservative_eta("A", "下週", df)
    assert r["available"] is False


def test_nan_delay_days_are_ignored_and_not_counted():
    """
    髒資料（收貨紀錄缺日期）不該悄悄拉低樣本門檻，也不該混進百分位計算。
    25 筆改期單裡有 5 筆是 NaN，應視為只有 20 筆可用資料。
    """
    df = _outcomes([("A", 10, 2)] * 20 + [("A", float("nan"), 2)] * 5)
    est = supplier_stats.estimate_delay(df, "A", percentile=0.80)
    assert est["available"] and est["basis"] == "改期過的單"
    assert est["n"] == 20 and est["delay_days"] == 10


def test_object_dtype_with_none_does_not_raise():
    """
    有些來源（例如手動組的 DataFrame）欄位是 object dtype、混了 None，
    不是乾淨的數值欄；估計函式不該因此丟例外。
    """
    df = pd.DataFrame(
        {"supplier_id": ["A"] * 25,
         "delay_days": pd.array([10] * 20 + [None] * 5, dtype=object),
         "reschedule_count": [2] * 25})
    est = supplier_stats.estimate_delay(df, "A", percentile=0.80)
    assert est["available"] and est["n"] == 20 and est["delay_days"] == 10


# ---------------------------------------------------------------------------
# 讀真實（模擬）資料庫
# ---------------------------------------------------------------------------
DB = ROOT / "data" / "erp_sim.db"
needs_db = pytest.mark.skipif(
    not DB.exists(),
    reason="需先執行 py src/build_erp_db.py 與 py src/generate_history.py")


@needs_db
def test_load_outcomes_has_columns_the_backtest_needs():
    df = supplier_stats.load_outcomes()
    assert {"po_no", "supplier_id", "committed_date", "receipt_date", "need_date",
            "notice_date", "vendor_otd", "delay_days", "reschedule_count"} <= set(df.columns)
    assert len(df) > 100


@needs_db
def test_notice_date_only_exists_for_rescheduled_orders():
    df = supplier_stats.load_outcomes()
    assert df.loc[df["reschedule_count"] >= 1, "notice_date"].notna().all()
    assert df.loc[df["reschedule_count"] == 0, "notice_date"].isna().all()


@needs_db
def test_supplier_performance_flags_small_samples():
    """
    樣本太少的統計會誤導人。只有三張單的供應商算出「準交率 33%」，
    那個數字不該拿去做決策，工具寧可說「樣本不足」也不要給假訊號。
    """
    perf = supplier_stats.supplier_performance(min_samples=1000)
    assert not perf["樣本是否足夠"].any()
    perf = supplier_stats.supplier_performance(min_samples=1)
    assert perf["樣本是否足夠"].all()
    assert perf["準交率"].between(0, 1).all()


@needs_db
def test_conservative_eta_is_never_earlier_than_promised_on_real_history():
    outcomes = supplier_stats.load_outcomes()
    for sid in outcomes["supplier_id"].unique():
        r = supplier_stats.conservative_eta(sid, "2026-10-29", outcomes)
        if r["available"]:
            assert r["conservative"] >= r["promised"], sid


@needs_db
def test_reschedule_reliability_supports_using_rescheduled_orders_only():
    """
    這張表是「只用改期過的單」的依據：改期越多次，最終仍延遲的比例應越高。
    若這個關係不成立，估計就不該把改期單獨立出來。
    """
    t = supplier_stats.reschedule_reliability().set_index("改期情形")
    if {"未改期", "改期 3 次以上"} <= set(t.index):
        assert t.loc["改期 3 次以上", "最終仍延遲比例"] > t.loc["未改期", "最終仍延遲比例"]

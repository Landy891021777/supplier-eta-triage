# -*- coding: utf-8 -*-
"""
回測的測試。守住的只有一件事：**不可以偷看未來。**

偷看未來的回測數字會漂亮得不像話，而上線時根本沒有那些資料。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import backtest  # noqa: E402


def _row(po, sup, delay, resched, committed, receipt, need, notice, otd=0.85):
    return {"po_no": po, "supplier_id": sup, "delay_days": delay,
            "reschedule_count": resched, "committed_date": committed,
            "receipt_date": receipt, "need_date": need,
            "notice_date": notice, "vendor_otd": otd}


def _toy() -> pd.DataFrame:
    """A 供應商：上半年 30 張改期單全都準時；下半年才開始變糟（延 20 天）。"""
    rows = [_row(f"E{i}", "A", 0, 1, "2026-01-20", "2026-01-20", "2026-02-20",
                 "2026-01-10") for i in range(30)]
    rows += [_row(f"L{i}", "A", 20, 1, "2026-07-01", "2026-07-21", "2026-08-01",
                  "2026-06-20") for i in range(30)]
    # 唯一的測試單：通知日 2026-05-01，此時只知道上半年那些全準時的單
    rows.append(_row("T1", "A", 20, 1, "2026-05-10", "2026-05-30", "2026-05-25",
                     "2026-05-01"))
    return pd.DataFrame(rows)


def test_training_slice_only_contains_orders_already_received():
    df = _toy()
    train = backtest.training_slice(df, "2026-05-01")
    assert (pd.to_datetime(train["receipt_date"]) < pd.Timestamp("2026-05-01")).all()
    assert "T1" not in set(train["po_no"])
    assert not any(p.startswith("L") for p in train["po_no"])


def test_rolling_backtest_does_not_see_the_future():
    """
    回歸測試（防偷看）：T1 在 5/1 收到改期通知，當時 A 供應商過去的單全都
    準時，估計應為 0 天。若誤把下半年那 30 張延 20 天的單算進去，
    估計會變成 20 天，涵蓋率因此看起來很漂亮 —— 但那是作弊。
    """
    bt = backtest.rolling_backtest(_toy(), test_from="2026-05-01", min_samples=20)
    t1 = bt[bt["po_no"] == "T1"]
    assert len(t1) == 1
    assert t1.iloc[0]["est_80"] == 0


def test_coverage_counts_only_orders_with_an_estimate():
    bt = pd.DataFrame({"delay_days": [0, 5, 30], "est_80": [4, 4, None],
                       "est_90": [4, 4, None], "est_95": [4, 4, None]})
    rate, n = backtest.coverage(bt, 0.80)
    assert n == 2 and rate == 0.5


def test_precision_at_k_uses_the_ranking_given():
    bt = pd.DataFrame({"gap_ours": [9, 1, 5, 3], "actual_short": [True, False, True, False]})
    assert backtest.precision_at_k(bt, "gap_ours", k=2) == 1.0
    assert backtest.precision_at_k(bt, "gap_ours", k=2, ascending=True) == 0.0


def test_ranking_table_always_reports_the_random_baseline():
    bt = pd.DataFrame({"gap_ours": [3, 2, 1, 0], "gap_buffer": [0, 1, 2, 3],
                       "gap_vendor": [0.1, 0.2, 0.3, 0.4],
                       "actual_short": [True, False, True, False]})
    t = backtest.ranking_table(bt, frac=0.5)
    assert any("隨機" in s for s in t["方法"])
    assert t["前段命中率"].between(0, 1).all()

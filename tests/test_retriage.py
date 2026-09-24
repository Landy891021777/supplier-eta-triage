# -*- coding: utf-8 -*-
"""
retriage：企劃調整收貨處理天數後，只重算分級，不重跑讀信。

守住的是「調了就生效、而且沒有偷偷呼叫 LLM」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline  # noqa: E402

TCFG = {"tight_buffer_days": 3, "percentile_confirmed": .8,
        "percentile_estimated": .9, "percentile_intent_only": .95, "min_samples": 20}


def _all():
    base = dict(matched=True, change_type="delay", commitment_strength="confirmed",
                committed_date="2026-10-01", need_date="2026-10-20",
                downstream_scheduled=False, has_second_source=True, is_bottleneck=False,
                alt_material_id="", category="PHOTORESIST", gr_processing_days=0,
                delay_days_est=0, delay_basis="改期過的單", delay_n=40, delay_percentile=.8,
                received_at=pd.Timestamp("2026-09-08"), needs_human_review=False,
                supplier_id="SUP-R01")
    return pd.DataFrame([
        {**base, "po_no": "A", "material_id": "PR-ArF-1088", "new_eta": "2026-10-18"},
        {**base, "po_no": "B", "material_id": "PR-ArF-2000", "new_eta": "2026-10-05"},
    ])


def test_override_changes_priority_without_rerunning_extraction(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("retriage 不可重跑讀信")
    monkeypatch.setattr(pipeline, "extract_one", boom)
    base = pipeline.retriage(_all(), {}, TCFG)
    assert base.set_index("po_no").loc["A", "priority"] == "P3"
    changed = pipeline.retriage(_all(), {"PR-ArF-1088": (5, "企劃 王小明 09-22 調整：全檢")}, TCFG)
    row = changed.set_index("po_no").loc["A"]
    assert row["gap_days"] == 3 and row["priority"] == "P2"
    assert any("王小明" in s for s in row["reasons"])


def test_unmatched_rows_pass_through_untouched():
    df = pd.concat([_all(), pd.DataFrame([{"po_no": "X", "matched": False, "priority": "待查",
                                           "reasons": ["對不到"], "actions": [],
                                           "received_at": pd.Timestamp("2026-09-08")}])])
    out = pipeline.retriage(df, {}, TCFG)
    assert out.set_index("po_no").loc["X", "priority"] == "待查"


def test_retriage_infers_estimate_from_legacy_delay_n_when_flags_missing():
    """
    相容舊格式 all_df（沒有 estimate_available／estimate_reason 這兩個
    原始欄位）：delay_n > 0 代表算過估計，百分位缺的話就照承諾強度現算，
    不能整筆當成「沒有估計」直接把天數蓋掉——那會讓舊格式重算出來的
    分級系統性偏保守（缺料天數用 0 天延遲去算，變得比實際更樂觀或更悲觀
    都是錯的，重點是不該偷偷丟掉已經有的歷史落差資訊）。
    """
    df = _all().drop(columns=["delay_percentile"])  # 模擬舊格式沒有這個欄位
    out = pipeline.retriage(df, {}, TCFG)
    row = out.set_index("po_no").loc["A"]
    assert row["priority"] == "P3"  # confirmed -> percentile_confirmed，跟原本一致


def test_retriage_treats_zero_delay_n_as_no_estimate_when_flags_missing():
    """delay_n == 0 代表真的沒有歷史樣本，這種才該落回「沒有估計」。"""
    df = _all()
    df.loc[df["po_no"] == "A", "delay_n"] = 0
    out = pipeline.retriage(df, {}, TCFG)
    row = out.set_index("po_no").loc["A"]
    assert any("沒有歷史收貨紀錄" in s for s in row["reasons"])

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

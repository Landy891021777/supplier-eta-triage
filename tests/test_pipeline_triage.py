# -*- coding: utf-8 -*-
"""
端到端：信件 → 對位 → 預估缺料天數 → 分級排序。

只跑規則層（use_llm=False），完全離線，不呼叫任何 API。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

pytestmark = pytest.mark.skipif(
    not ((ROOT / "data" / "erp_sim.db").exists()
         and (ROOT / "data" / "inbox" / "_index.json").exists()),
    reason="需先執行 generate_data.py、build_erp_db.py、generate_history.py")


@pytest.fixture(scope="module")
def result():
    import pipeline
    return pipeline.run(use_llm=False)


def test_weighted_score_is_gone(result):
    cols = set(result["actions"].columns)
    assert "impact_score" not in cols and "rule_details" not in cols
    assert {"gap_days", "conservative_eta", "reasons", "actions"} <= cols


def test_list_is_sorted_by_tier_then_shortage_days(result):
    from triage import PRIORITY_RANK
    df = result["actions"]
    ranks = df["priority"].map(PRIORITY_RANK).tolist()
    assert ranks == sorted(ranks), "分級必須由 P1 排到 P3"
    for tier, g in df.groupby("priority"):
        gaps = g["gap_days"].dropna().tolist()
        assert gaps == sorted(gaps, reverse=True), f"{tier} 內須依缺料天數由大到小"


def test_p1_means_shortage_and_a_critical_flag(result):
    p1 = result["actions"][result["actions"]["priority"] == "P1"]
    assert (p1["gap_days"] > 0).all()
    critical = p1["downstream_scheduled"].astype(bool) | p1["is_bottleneck"].astype(bool)
    assert critical.all()


def test_every_row_explains_itself(result):
    df = result["actions"]
    assert df["reasons"].map(len).gt(0).all()


def test_unconfirmed_dates_still_need_human_review(result):
    """承諾強度非 confirmed 的一律不寫回，這道閘門不能因為改版而消失。"""
    df = result["actions"]
    weak = df["commitment_strength"].isin(["estimated", "intent_only"])
    assert df.loc[weak, "needs_human_review"].all()

# -*- coding: utf-8 -*-
"""
端到端：信件 → 對位 → 預估缺料天數 → 分級排序。

只跑規則層（use_llm=False），完全離線，不呼叫任何 API。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
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


def test_retriage_reproduces_runs_own_per_row_evaluation(result):
    """
    run() 對每一列直接呼叫一次 triage.evaluate（寫進 result["all"]）；
    retriage() 是另一條路徑——從 all_df 重建 record/po/material/estimate
    再呼叫 triage.evaluate 一次。這兩條路徑必須算出一樣的結果，否則
    企劃調完收貨處理天數、呼叫 retriage() 重算時，看到的分級會跟
    run() 剛產生的首頁對不上，而且沒有人會發現，因為兩邊各自看起來
    都「正常執行完畢」。

    比較的是 all_df 裡（run() 自己算的）欄位，不是 result["actions"]——
    result["actions"] 本身就是拿 retriage(all, {}, tcfg) 的回傳值，
    拿它跟 retriage(all, {}, tcfg) 比較沒有意義：兩邊根本是同一次呼叫，
    不管 run() 或 retriage() 算錯了什麼，這種比法永遠會「一致」。
    """
    import pipeline
    tcfg = pipeline.load_config()["triage"]
    all_df = result["all"]
    matched = all_df[all_df["matched"]].reset_index(drop=True)
    assert len(matched) > 0, "測試資料裡沒有對到 PO 的列，這個一致性測試量不到東西"

    replay = pipeline.retriage(all_df, {}, tcfg).set_index("po_no")

    checked = 0
    for _, row in matched.iterrows():
        po_no = row["po_no"]
        if row["priority"] == "—":
            assert po_no not in replay.index, po_no
            continue
        r = replay.loc[po_no]
        assert r["priority"] == row["priority"], po_no
        # 「待查」時 gap_days 是 None／NaN，NaN != NaN，要分開比較。
        same_gap = ((pd.isna(r["gap_days"]) and pd.isna(row["gap_days"]))
                   or r["gap_days"] == row["gap_days"])
        assert same_gap, po_no
        assert r["reasons"] == row["reasons"], po_no
        assert r["actions"] == row["actions"], po_no
        checked += 1
    assert checked > 0, "沒有任何一列走到有分級的比較，這個一致性測試量不到東西"

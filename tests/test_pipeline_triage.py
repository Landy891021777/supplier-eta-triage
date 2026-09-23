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


# ---------------------------------------------------------------------------
# match_schedule_line：對位到排程行的四條規則（Plan 3 Task 3）
# ---------------------------------------------------------------------------
def test_match_schedule_line_rule1_matches_by_qty():
    """規則 1：抽取結果有 qty，且某一行的 sched_qty 與它相同 → 對到那一行。"""
    import pipeline
    lines = [{"sched_line": 1, "sched_qty": 8, "committed_date": "2026-09-30"},
             {"sched_line": 2, "sched_qty": 12, "committed_date": "2026-11-15"}]
    sched_line, note, needs_review = pipeline.match_schedule_line(
        {"qty": 12, "new_eta": "2026-11-15"}, lines)
    assert sched_line == 2 and note == "" and not needs_review


def test_match_schedule_line_rule2_matches_by_date_when_no_qty():
    """規則 2：沒有 qty，但信中的日期跟某一行的 committed_date 相同 → 對到那一行。"""
    import pipeline
    lines = [{"sched_line": 1, "sched_qty": 8, "committed_date": "2026-09-30"},
             {"sched_line": 2, "sched_qty": 12, "committed_date": "2026-11-15"}]
    sched_line, note, needs_review = pipeline.match_schedule_line(
        {"qty": None, "new_eta": "2026-09-30"}, lines)
    assert sched_line == 1 and note == "" and not needs_review


def test_match_schedule_line_rule3_single_line_needs_no_comparison():
    """規則 3：這張單只有一行，不必比對，行為跟改版前完全一樣。"""
    import pipeline
    lines = [{"sched_line": 1, "sched_qty": 20, "committed_date": "2026-09-30"}]
    # 故意給一個對不上（qty=999）的抽取結果，只有一行時仍要對到它——
    # 這是「單一排程行的單，行為完全不變」這條相容規則。
    sched_line, note, needs_review = pipeline.match_schedule_line(
        {"qty": 999, "new_eta": "2026-01-01"}, lines)
    assert sched_line == 1 and note == "" and not needs_review


def test_match_schedule_line_rule4_falls_back_to_earliest_open_line():
    """
    規則 4（保守退路）：qty／日期都對不上任何一行時，對到最早的未交行，
    並強制 needs_human_review——寧可多一步人工確認，也不要把日期套錯批次。
    """
    import pipeline
    lines = [{"sched_line": 2, "sched_qty": 12, "committed_date": "2026-11-15"},
             {"sched_line": 1, "sched_qty": 8, "committed_date": "2026-09-30"}]
    sched_line, note, needs_review = pipeline.match_schedule_line(
        {"qty": 999, "new_eta": "2026-01-01"}, lines)
    assert sched_line == 1              # 最早的未交行（committed_date 較早那筆）
    assert "未指明是哪一批" in note
    assert needs_review


def test_match_schedule_line_requires_at_least_one_line():
    import pipeline
    with pytest.raises(ValueError):
        pipeline.match_schedule_line({"qty": 1}, [])


# ---------------------------------------------------------------------------
# HC-010 端到端：分批交貨，兩批各自分級
# ---------------------------------------------------------------------------
def test_hc010_partial_delivery_produces_two_rows_first_ok_second_short(result):
    """
    決策 19 的端到端驗證：HC-010（PO-2026-04188 分批交貨，8 片照原日期、
    12 片延到 11/15）解析＋對位後，要看到兩個獨立的排程行結果——不能只取
    最晚一筆（那會把準時的 8 片也一起當成缺料，或反過來完全看不到延遲
    的 12 片）。
    """
    all_df = result["all"]
    sub = all_df[all_df["po_no"] == "PO-2026-04188"].set_index("sched_line")
    assert set(sub.index) == {1, 2}
    assert sub.loc[1, "sched_qty"] == 8 and sub.loc[2, "sched_qty"] == 12
    assert sub.loc[1, "sched_lines_total"] == 2 == sub.loc[2, "sched_lines_total"]

    # 第 1 批（8 片、照原日期到）：不缺料，判為「—」不進行動清單。
    assert sub.loc[1, "priority"] == "—"

    # 第 2 批（12 片、延到 11/15）：明顯缺料，要進行動清單。
    assert sub.loc[2, "priority"] in ("P1", "P2", "P3")
    assert sub.loc[2, "gap_days"] > 0

    actionable_pos = result["actions"][result["actions"]["po_no"] == "PO-2026-04188"]
    assert len(actionable_pos) == 1
    assert actionable_pos.iloc[0]["sched_line"] == 2


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

    # 鍵是 (po_no, sched_line)，不是單純 po_no：分批交貨的單同一個 po_no
    # 會有兩列，用 po_no 當索引在 .set_index() 之後會出現重複索引，
    # .loc[po_no] 撈到的可能是另一批的列，比對永遠對不上（見決策 19）。
    replay = pipeline.retriage(all_df, {}, tcfg).set_index(["po_no", "sched_line"])

    checked = 0
    for _, row in matched.iterrows():
        key = (row["po_no"], row["sched_line"])
        if row["priority"] == "—":
            assert key not in replay.index, key
            continue
        r = replay.loc[key]
        assert r["priority"] == row["priority"], key
        # 「待查」時 gap_days 是 None／NaN，NaN != NaN，要分開比較。
        same_gap = ((pd.isna(r["gap_days"]) and pd.isna(row["gap_days"]))
                   or r["gap_days"] == row["gap_days"])
        assert same_gap, key
        assert r["reasons"] == row["reasons"], key
        assert r["actions"] == row["actions"], key
        checked += 1
    assert checked > 0, "沒有任何一列走到有分級的比較，這個一致性測試量不到東西"

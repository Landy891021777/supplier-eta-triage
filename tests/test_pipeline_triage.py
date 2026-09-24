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
# _resolve_record_batches：一封信裡同一張單撞在同一個排程行上
# （分批交貨收尾修正：提議拆批 vs. 規則 4 猜錯是兩件不同的事）
# ---------------------------------------------------------------------------
def test_resolve_record_batches_single_record_no_collision():
    """一封信對這張單只有一筆記錄：跟改版前完全一樣，batch_key 空字串。"""
    import pipeline
    po = {"lines": [{"sched_line": 1, "sched_qty": 20, "committed_date": "2026-09-30"}]}
    out = pipeline._resolve_record_batches([{"qty": None, "new_eta": "2026-09-30"}], po)
    assert len(out) == 1
    assert out[0]["sched_line"] == 1
    assert out[0]["batch_key"] == ""
    assert not out[0]["is_proposed_split"]


def test_resolve_record_batches_proposed_split_when_po_has_one_line():
    """
    PO 只有一筆排程行，但這封信對它抽出兩筆記錄（供應商提議拆批）：
    兩筆都要對到 sched_line=1，is_proposed_split=True，batch_key 用 qty
    區分，不能讓後面那筆蓋掉前面那筆。
    """
    import pipeline
    po = {"lines": [{"sched_line": 1, "sched_qty": 20, "committed_date": "2026-09-30"}]}
    records = [{"qty": 8, "new_eta": "2026-09-30"}, {"qty": 12, "new_eta": "2026-11-15"}]
    out = pipeline._resolve_record_batches(records, po)
    assert [o["sched_line"] for o in out] == [1, 1]
    assert [o["batch_key"] for o in out] == ["qty8", "qty12"]
    assert all(o["is_proposed_split"] for o in out)
    assert all(o["needs_line_review"] for o in out)
    assert [o["batch_index"] for o in out] == [1, 2]
    assert [o["batch_total"] for o in out] == [2, 2]


def test_resolve_record_batches_duplicate_qty_still_gets_distinct_keys():
    """50/50 對分的兩批 qty 剛好相同時，batch_key 仍要彼此不同。"""
    import pipeline
    po = {"lines": [{"sched_line": 1, "sched_qty": 20, "committed_date": "2026-09-30"}]}
    records = [{"qty": 10, "new_eta": "2026-09-30"}, {"qty": 10, "new_eta": "2026-11-15"}]
    out = pipeline._resolve_record_batches(records, po)
    keys = [o["batch_key"] for o in out]
    assert len(set(keys)) == 2, keys


def test_resolve_record_batches_rule4_collision_on_multiline_po_is_not_proposed_split():
    """
    PO 本來就有兩筆排程行，兩筆記錄都猜不出是哪一批、規則 4 剛好都退到
    同一個最早的未交行——這不是「供應商提議拆批」（ERP 早就拆好了），
    要維持各自「對到最早的一批」的說法，不能被覆寫成提議拆批的文字。
    """
    import pipeline
    po = {"lines": [{"sched_line": 1, "sched_qty": 8, "committed_date": "2026-09-30"},
                    {"sched_line": 2, "sched_qty": 12, "committed_date": "2026-11-15"}]}
    # 兩筆都給對不上任何一行的 qty／日期，逼它們都落到規則 4。
    records = [{"qty": 999, "new_eta": "2026-01-01"}, {"qty": 888, "new_eta": "2026-02-02"}]
    out = pipeline._resolve_record_batches(records, po)
    assert [o["sched_line"] for o in out] == [1, 1]
    assert not any(o["is_proposed_split"] for o in out)
    assert all("未指明是哪一批" in o["match_note"] for o in out)
    assert len(set(o["batch_key"] for o in out)) == 2, "仍要彼此不同，不能互相蓋掉"


# ---------------------------------------------------------------------------
# _keep_latest_email_per_schedule_line：latest email wins
# ---------------------------------------------------------------------------
def test_latest_email_wins_even_when_older_email_had_more_batches():
    """
    分批交貨收尾修正的迴歸測試：較舊的一封信把同一個排程行拆成兩批
    （供應商提議拆批），較新的一封信改口只給單一日期——舊信的兩批都要
    整組被換掉，只留新信那一列，不能因為舊信「筆數比較多」就留下來跟
    新信並存。
    """
    import pipeline
    df = pd.DataFrame([
        {"po_no": "A", "sched_line": 1, "batch_key": "qty8",
         "received_at": pd.Timestamp("2026-09-08 09:00"), "marker": "old-1"},
        {"po_no": "A", "sched_line": 1, "batch_key": "qty12",
         "received_at": pd.Timestamp("2026-09-08 09:00"), "marker": "old-2"},
        {"po_no": "A", "sched_line": 1, "batch_key": "",
         "received_at": pd.Timestamp("2026-09-10 09:00"), "marker": "new"},
    ])
    out = pipeline._keep_latest_email_per_schedule_line(df)
    assert list(out["marker"]) == ["new"]


def test_latest_email_wins_keeps_all_batches_from_the_latest_email():
    """反過來：最新一封信才是拆批的那封，兩批都要留下，不能只留一批。"""
    import pipeline
    df = pd.DataFrame([
        {"po_no": "A", "sched_line": 1, "batch_key": "",
         "received_at": pd.Timestamp("2026-09-08 09:00"), "marker": "old"},
        {"po_no": "A", "sched_line": 1, "batch_key": "qty8",
         "received_at": pd.Timestamp("2026-09-10 09:00"), "marker": "new-1"},
        {"po_no": "A", "sched_line": 1, "batch_key": "qty12",
         "received_at": pd.Timestamp("2026-09-10 09:00"), "marker": "new-2"},
    ])
    out = pipeline._keep_latest_email_per_schedule_line(df)
    assert set(out["marker"]) == {"new-1", "new-2"}


def test_latest_email_wins_keeps_unmatched_rows_with_none_sched_line():
    """
    未對到主檔的列 sched_line 是 None：groupby 必須用 dropna=False，
    否則這些列會被排除在任何一組之外、判成 NaN，永遠留不下來。
    """
    import pipeline
    df = pd.DataFrame([
        {"po_no": "ZZZ-NOT-FOUND", "sched_line": None, "batch_key": "",
         "received_at": pd.Timestamp("2026-09-08 09:00"), "marker": "unmatched"},
    ])
    out = pipeline._keep_latest_email_per_schedule_line(df)
    assert list(out["marker"]) == ["unmatched"]


# ---------------------------------------------------------------------------
# HC-010 端到端：分批交貨，兩批各自分級
# ---------------------------------------------------------------------------
def test_hc010_partial_delivery_produces_two_rows_first_ok_second_short(result):
    """
    決策 19 的端到端驗證（分批交貨收尾修正版）：PO-2026-04188 在 ERP 裡
    是**單一**排程行（20 片 @ 2026-09-30，尚未拆行），HC-010 的信是供應商
    「提議」拆成 8 片照原日期、12 片延到 11/15——兩批都要對到同一個
    sched_line=1（ERP 沒變），但彼此不能互相覆蓋：工具要看到兩個獨立的
    結果，不能只取最晚一筆（那會把準時的 8 片也一起當成缺料，或反過來
    完全看不到延遲的 12 片）。
    """
    all_df = result["all"]
    sub = (all_df[all_df["po_no"] == "PO-2026-04188"]
          .set_index("batch_key"))
    assert set(sub.index) == {"qty8", "qty12"}
    assert (sub["sched_line"] == 1).all(), "ERP 那筆排程行沒有被拆開，兩批都該對到同一行"
    assert sub.loc["qty8", "sched_qty"] == 8 and sub.loc["qty12", "sched_qty"] == 12
    assert sub["is_proposed_split"].all(), "兩批都該被標記為供應商提議拆批"
    assert sub["needs_human_review"].all(), "提議拆批的兩批都要人工確認"
    assert all("提議把這一行拆成 2 批" in r[0] for r in sub["reasons"])

    # 第 1 批（8 片、照原日期到）：不缺料，判為「—」不進行動清單。
    assert sub.loc["qty8", "priority"] == "—"
    assert sub.loc["qty8", "change_type"] == "no_change"

    # 第 2 批（12 片、延到 11/15）：明顯缺料，要進行動清單，change_type
    # 是 delay（跟 ERP 那筆排程行原本的承諾日 2026-09-30 比，真的晚了）。
    assert sub.loc["qty12", "change_type"] == "delay"
    assert sub.loc["qty12", "priority"] in ("P1", "P2", "P3")
    assert sub.loc["qty12", "gap_days"] > 0

    actionable_pos = result["actions"][result["actions"]["po_no"] == "PO-2026-04188"]
    assert len(actionable_pos) == 1, "只有延遲那一批缺料，準時的一批不進行動清單"
    assert actionable_pos.iloc[0]["batch_key"] == "qty12"


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

    # 鍵是 (po_no, sched_line, batch_key)，不是只有 (po_no, sched_line)：
    # 供應商提議拆批時，同一張單同一個排程行會有兩列（見決策 19 的修正），
    # 只用前兩個當索引在 .set_index() 之後會出現重複索引，.loc[key] 撈到
    # 的可能是另一批的列，比對永遠對不上。
    replay = pipeline.retriage(all_df, {}, tcfg).set_index(["po_no", "sched_line", "batch_key"])

    checked = 0
    for _, row in matched.iterrows():
        key = (row["po_no"], row["sched_line"], row["batch_key"])
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

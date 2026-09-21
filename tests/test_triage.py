# -*- coding: utf-8 -*-
"""
交期風險分級的測試。

守住的是「判斷方向」與「不亂建議」：
  - 缺料與否由日期相減決定，不是分數
  - 建議動作遵守 AVL：替代料未驗證前不可寫成可用
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from domain import ChangeType, CommitmentStrength  # noqa: E402
from triage import (PRIORITY_RANK, _clean_str, evaluate,  # noqa: E402
                    percentile_for)

CFG = {"tight_buffer_days": 3, "percentile_confirmed": 0.80,
       "percentile_estimated": 0.90, "percentile_intent_only": 0.95}

PO = {"committed_date": "2026-10-01", "need_date": "2026-10-20",
      "downstream_scheduled": False}
MAT = {"has_qualified_second_source": True, "is_bottleneck": False,
       "alt_material_id": ""}


def _est(days: int) -> dict:
    return {"available": True, "delay_days": days, "percentile": 0.80,
            "basis": "改期過的單", "n": 40}


def _run(new_eta="2026-10-10", est=None, po=None, mat=None, rec=None):
    record = {"new_eta": new_eta, "change_type": ChangeType.DELAY.value,
              "commitment_strength": CommitmentStrength.CONFIRMED.value,
              **(rec or {})}
    return evaluate(record, {**PO, **(po or {})}, {**MAT, **(mat or {})}, CFG,
                    _est(0) if est is None else est)


# ---------------------------------------------------------------------------
# 預估缺料天數
# ---------------------------------------------------------------------------
def test_history_delay_turns_a_safe_looking_order_into_a_shortage():
    """
    供應商說 10/10、需求日 10/20，表面上還有 10 天緩衝。
    但這家供應商說定後通常再晚 15 天 → 保守到料日 10/25，缺 5 天。
    只看供應商說的日期會漏掉這張單。
    """
    r = _run(est=_est(15))
    assert r["conservative_eta"] == "2026-10-25"
    assert r["gap_days"] == 5
    assert r["priority"] == "P2"


def test_without_history_falls_back_to_stated_date_and_says_so():
    r = _run(est={"available": False, "n": 3,
                  "reason": "歷史樣本不足（3 筆），不提供保守估計"})
    assert r["gap_days"] == -10
    assert any("樣本不足" in s for s in r["reasons"])


def test_percentile_for_picks_more_conservative_when_not_confirmed():
    assert percentile_for("confirmed", CFG) == 0.80
    assert percentile_for("estimated", CFG) == 0.90
    assert percentile_for("intent_only", CFG) == 0.95
    assert percentile_for("none", CFG) == 0.95
    assert percentile_for(None, CFG) == 0.95


# ---------------------------------------------------------------------------
# 分級
# ---------------------------------------------------------------------------
def test_shortage_with_downstream_scheduled_is_p1():
    r = _run(est=_est(15), po={"downstream_scheduled": True})
    assert r["priority"] == "P1"


def test_shortage_on_bottleneck_material_is_p1():
    assert _run(est=_est(15), mat={"is_bottleneck": True})["priority"] == "P1"


def test_shortage_without_critical_flags_is_p2():
    assert _run(est=_est(15))["priority"] == "P2"


def test_tight_buffer_on_single_source_is_p2():
    r = _run(new_eta="2026-10-18", est=_est(0),
             mat={"has_qualified_second_source": False})
    assert r["gap_days"] == -2 and r["priority"] == "P2"


def test_tight_buffer_with_qualified_second_source_is_p3():
    r = _run(new_eta="2026-10-18", est=_est(0))
    assert r["priority"] == "P3"


def test_plenty_of_buffer_is_p3_even_for_single_source():
    r = _run(est=_est(0), mat={"has_qualified_second_source": False})
    assert r["priority"] == "P3"


def test_priority_rank_orders_tiers():
    assert PRIORITY_RANK["P1"] < PRIORITY_RANK["P2"] < PRIORITY_RANK["P3"]


def test_no_change_leaves_action_list():
    r = evaluate({"change_type": ChangeType.NO_CHANGE.value, "new_eta": None},
                 PO, MAT, CFG, _est(9))
    assert r["priority"] == "—" and r["gap_days"] is None


def test_pull_in_is_p3_with_warehouse_note():
    """提前交貨要處理倉容與付款，不該和斷料排在一起。"""
    r = evaluate({"change_type": ChangeType.PULL_IN.value, "new_eta": "2026-09-20"},
                 PO, {**MAT, "is_bottleneck": True}, CFG, _est(9))
    assert r["priority"] == "P3" and "倉容" in r["note"]


def test_missing_need_date_is_flagged_not_guessed():
    r = _run(po={"need_date": None})
    assert r["priority"] == "待查" and r["gap_days"] is None


def test_delay_without_new_date_is_never_left_at_p3():
    """
    供應商說會延、卻沒給新日期時，只能拿原承諾日去算，缺料天數一定被低估。
    這種單最需要企劃立刻追日期，不能因為「看起來還有緩衝」就掉到 P3。
    """
    r = _run(new_eta=None, est=_est(0),
             rec={"commitment_strength": CommitmentStrength.NONE.value})
    assert r["priority"] in ("P1", "P2")
    assert "未給新日期" in r["reasons"][0]
    assert "確切日期" in r["actions"][0]


# ---------------------------------------------------------------------------
# 建議動作（遵守 AVL）
# ---------------------------------------------------------------------------
def test_alternate_material_is_never_called_usable():
    """
    替代料要過客戶與品保驗證（AVL），不能想換就換。
    資料表只知道有替代料，不知道它驗證過沒有，所以只能請品保確認。
    """
    r = _run(est=_est(15), mat={"alt_material_id": "WF-N7-KL2211"})
    text = "；".join(r["actions"])
    assert "WF-N7-KL2211" in text and "品保" in text
    assert "可換料" not in text and "可直接" not in text


def test_nan_alternate_is_treated_as_none():
    """
    回歸測試：料號主檔的替代料欄位在 CSV 讀進 pandas 會變成 NaN，
    str(NaN) == "nan" 是非空字串，早期版本因此對企劃說「有替代料 nan」。
    """
    for empty in (float("nan"), None, "", "  ", "NaN", "None"):
        r = _run(est=_est(15), mat={"alt_material_id": empty})
        assert "替代料" not in "；".join(r["actions"]), repr(empty)
        assert "nan" not in "；".join(r["actions"]).lower()


def test_second_source_action_goes_through_procurement():
    r = _run(est=_est(15))
    assert any("採購" in a and "二源" in a for a in r["actions"])


def test_single_source_action_states_the_limit():
    r = _run(est=_est(15), mat={"has_qualified_second_source": False})
    assert any("單一來源" in a for a in r["actions"])


def test_downstream_scheduled_notifies_production_control():
    r = _run(est=_est(15), po={"downstream_scheduled": True})
    assert any("生管" in a for a in r["actions"])


def test_unconfirmed_date_asks_for_a_firm_date_first():
    r = _run(est=_est(15),
             rec={"commitment_strength": CommitmentStrength.INTENT_ONLY.value})
    assert "確切日期" in r["actions"][0]


def test_p3_has_no_actions():
    assert _run(est=_est(0))["actions"] == []


def test_actions_do_not_claim_cost_or_feasibility():
    r = _run(est=_est(15))
    hand = [a for a in r["actions"] if "空運" in a][0]
    assert "確認" in hand and "$" not in hand and "元" not in hand


def test_clean_str_normalises_empty_values():
    assert _clean_str(float("nan")) == ""
    assert _clean_str(None) == ""
    assert _clean_str("nan") == ""
    assert _clean_str("  WF-1  ") == "WF-1"

# -*- coding: utf-8 -*-
"""
交期風險分級的測試。

守住的是「判斷方向」與「不亂建議」：
  - 缺料與否由日期相減決定，不是分數
  - 建議動作遵守 AVL：替代料未驗證前不可寫成可用
  - 沒有新日期時，措辭要誠實標出「這是拿原承諾日算的」，不能假裝是供應商剛說的話
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from domain import ChangeType, CommitmentStrength  # noqa: E402
from triage import (PRIORITY_RANK, _clean_str, _d, _flag, _missing,  # noqa: E402
                    evaluate, percentile_for)

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


def test_fallback_basis_wording_is_readable():
    """
    supplier_stats.estimate_delay() 樣本不足時退回全部單，basis 回
    "全部單（改期單樣本不足）"。直接套進舊模板會唸成「過去全部單（改期單
    樣本不足）（22 筆）」，括號疊括號、企劃看不懂在講什麼，要換句話說。
    """
    est = {"available": True, "delay_days": 5, "percentile": 0.80,
           "basis": "全部單（改期單樣本不足）", "n": 22}
    r = _run(est=est)
    joined = "；".join(r["reasons"])
    assert "這家供應商過去全部單共 22 筆（改期單不足，改用全部單）" in joined
    assert "過去全部單（改期單樣本不足）（22 筆）" not in joined


def test_zero_delay_wording_says_on_time_not_zero_days():
    """delay_days 是 0 時說「0 天內到」很怪，要說「準時或提前到」。"""
    r = _run(est=_est(0))
    joined = "；".join(r["reasons"])
    assert "有 80% 準時或提前到" in joined
    assert "0 天內到" not in joined


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
    assert r["actions"] == []


def test_pull_in_is_p3_with_warehouse_note():
    """提前交貨要處理倉容與付款，不該和斷料排在一起。"""
    r = evaluate({"change_type": ChangeType.PULL_IN.value, "new_eta": "2026-09-20"},
                 PO, {**MAT, "is_bottleneck": True}, CFG, _est(9))
    assert r["priority"] == "P3" and "倉容" in r["note"]


def test_pull_in_still_late_is_tiered_normally():
    """
    「提前」到的日期如果還是晚於需求日，等於根本沒解決缺料，
    不該因為信被判成 PULL_IN 就走進輕鬆的倉容分支。
    """
    r = _run(new_eta="2026-10-25", est=_est(0),
             po={"committed_date": "2026-10-30", "need_date": "2026-10-20",
                 "downstream_scheduled": True},
             rec={"change_type": ChangeType.PULL_IN.value})
    assert r["priority"] == "P1" and r["gap_days"] == 5
    assert any("供應商已提前到 2026-10-25，但仍晚於下游需求日" in s
              for s in r["reasons"])


def test_missing_need_date_is_flagged_not_guessed():
    r = _run(po={"need_date": None})
    assert r["priority"] == "待查" and r["gap_days"] is None


def test_change_type_without_usable_date_is_flagged_for_manual_review():
    """
    change_type 判斷不出來（例如 unknown）又沒有新日期，代表信根本沒解析
    出變更內容。拿原承諾日硬套只有「已知是延遲」時才做，其他情況不能猜。
    """
    r = _run(new_eta=None, rec={"change_type": ChangeType.UNKNOWN.value})
    assert r["priority"] == "待查"
    assert r["gap_days"] is None
    assert r["reasons"] == ["信中對到採購單，但讀不出新日期或變更內容，需人工看信"]
    assert r["actions"] == []


def test_delay_without_new_date_is_never_left_at_p3():
    """
    供應商說會延、卻沒給新日期時，只能拿原承諾日去算，缺料天數一定被低估。
    這種單最需要企劃立刻追日期，不能因為「看起來還有緩衝」就掉到 P3。
    也不能假裝那是「供應商說」的日期——那是原承諾日，不是新承諾，
    措辭必須誠實標出來，不能讓企劃誤以為緩衝真的還在。
    """
    r = _run(new_eta=None, est=_est(0),
             rec={"commitment_strength": CommitmentStrength.NONE.value})
    assert r["priority"] in ("P1", "P2")
    assert "未給新日期" in r["reasons"][0]
    assert "確切日期" in r["actions"][0]
    assert all("供應商說" not in s for s in r["reasons"])
    assert any("不能當真" in s for s in r["reasons"])


def test_delay_with_nan_new_eta_triggers_the_no_date_p2_floor():
    """
    回歸測試：pandas 讀進來的空日期是 float NaN，不是 None，
    _d() 沒接住的話這條「沒給新日期」的保護就形同虛設。
    """
    r = _run(new_eta=float("nan"), est=_est(0))
    assert r["priority"] in ("P1", "P2")
    assert "未給新日期" in r["reasons"][0]


def test_gap_exactly_zero_has_no_buffer_wording():
    """緩衝天數剛好是 0 時，「尚有 0 天緩衝」讀起來很怪，要說「沒有緩衝」。"""
    r = _run(new_eta="2026-10-20", est=_est(0))
    assert r["gap_days"] == 0
    joined = "；".join(r["reasons"])
    assert "與下游需求日同一天到，沒有緩衝（未含進料檢驗時間）" in joined
    assert "尚有 0 天緩衝" not in joined


def test_gap_exactly_zero_single_source_says_no_buffer_not_zero_days_left():
    r = _run(new_eta="2026-10-20", est=_est(0),
             mat={"has_qualified_second_source": False})
    assert r["priority"] == "P2"
    joined = "；".join(r["reasons"])
    assert "單一來源且沒有緩衝（門檻 3 天）" in joined
    assert "緩衝只剩 0 天" not in joined


# ---------------------------------------------------------------------------
# 布林旗標與空值正規化（M1／M2）
# ---------------------------------------------------------------------------
def test_string_false_downstream_flag_is_not_treated_as_true():
    """
    回歸測試：CSV／ERP 讀進來的布林欄位常是字串。bool("False") 是 True，
    早期版本會把「沒有下游排程」誤判成「有」，白白把 P2 升成 P1。
    """
    r = _run(est=_est(15), po={"downstream_scheduled": "False"})
    assert r["priority"] == "P2"


def test_nan_second_source_flag_is_treated_as_single_source():
    r = _run(new_eta="2026-10-18", est=_est(0),
             mat={"has_qualified_second_source": float("nan")})
    assert r["gap_days"] == -2 and r["priority"] == "P2"


def test_flag_normalises_various_truthy_and_falsy_inputs():
    assert _flag(True) is True
    assert _flag("true") is True
    assert _flag("Yes") is True
    assert _flag("是") is True
    assert _flag("False") is False
    assert _flag("0") is False
    assert _flag(None) is False
    assert _flag(float("nan")) is False
    assert _flag(1) is True
    assert _flag(0) is False


def test_d_and_clean_str_and_missing_handle_pandas_na():
    """
    pd.NA 的 `!=` 比較走三態邏輯、丟 TypeError，跟 float('nan') 不一樣；
    _missing() 沒有 try/except 兜底的話，_d／_clean_str 在真實 ERP
    資料（pandas 的 Nullable dtype）上會直接炸掉，不是回傳空值。
    """
    import pandas as pd

    assert _missing(pd.NA) is True
    assert _d(pd.NA) is None
    assert _clean_str(pd.NA) == ""


# ---------------------------------------------------------------------------
# 建議動作（遵守 AVL；P1／P2 一定有事可做）
# ---------------------------------------------------------------------------
def test_alternate_material_is_never_called_usable():
    """
    替代料要過客戶與品保驗證（AVL），不能想換就換。
    資料表只知道有替代料，不知道它驗證過沒有，所以只能請品保確認。
    """
    r = _run(est=_est(15), mat={"alt_material_id": "SW-300-P-2211"})
    text = "；".join(r["actions"])
    assert "SW-300-P-2211" in text and "品保" in text
    assert "可換料" not in text and "可直接" not in text
    assert "未確認前不可視為可用" in text


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


def test_downstream_scheduled_p1_notifies_production_control_before_catch_up_methods():
    """企劃今天就能做的事（通知生管）要排在需要跟採購對過才能開口的手段之前。"""
    r = _run(est=_est(15), po={"downstream_scheduled": True})
    notify_idx = next(i for i, a in enumerate(r["actions"]) if "生管" in a)
    catchup_idx = next(i for i, a in enumerate(r["actions"]) if "催貨" in a)
    assert notify_idx < catchup_idx


def test_unconfirmed_date_asks_for_a_firm_date_first():
    r = _run(est=_est(15),
             rec={"commitment_strength": CommitmentStrength.INTENT_ONLY.value})
    assert "確切日期" in r["actions"][0]


def test_delay_without_date_but_confirmed_strength_still_asks_for_a_date():
    """
    就算 commitment_strength 是 confirmed，沒有新日期本身就必須追日期——
    「確認」的是舊的承諾，不是新的變更。
    """
    r = _run(new_eta=None,
             rec={"commitment_strength": CommitmentStrength.CONFIRMED.value})
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


@pytest.mark.parametrize("kwargs", [
    dict(est=_est(15)),
    dict(est=_est(15), po={"downstream_scheduled": True}),
    dict(est=_est(15), mat={"is_bottleneck": True}),
    dict(new_eta="2026-10-18", est=_est(0),
         mat={"has_qualified_second_source": False}),
    dict(new_eta=None, est=_est(0)),
])
def test_every_p1_p2_scenario_gets_at_least_one_action(kwargs):
    """
    企劃不該看到「分級是 P1／P2，但沒有任何建議動作」的單——那等於
    工具告訴他有風險卻不告訴他能做什麼。
    """
    r = _run(**kwargs)
    assert r["priority"] in ("P1", "P2")
    assert len(r["actions"]) >= 1


# ---------------------------------------------------------------------------
# 收貨處理天數（可投產日 = 保守到料日 + 收貨處理天數）
# ---------------------------------------------------------------------------
def test_gr_processing_days_push_arrival_to_usable_date():
    """
    料 10/18 到、需求日 10/20，看似還有 2 天；但光阻到廠要 2 天檢驗＋回溫，
    10/20 才能投產 → 沒有緩衝。不算收貨處理時間會把這張單看得太樂觀。
    """
    r = _run(new_eta="2026-10-18", est=_est(0),
             mat={"gr_processing_days": 2, "gr_source": "光阻料別預設",
                  "has_qualified_second_source": False})
    assert r["gap_days"] == 0 and r["available_date"] == "2026-10-20"
    assert r["priority"] == "P2"   # 單一來源且沒有緩衝
    assert any("收貨處理 2 天" in s and "光阻料別預設" in s for s in r["reasons"])


def test_zero_gr_days_adds_no_reason_line():
    r = _run(est=_est(0), mat={"gr_processing_days": 0, "gr_source": "x"})
    assert not any("收貨處理" in s for s in r["reasons"])


def test_shortage_within_gr_days_suggests_expedited_inspection():
    """缺的天數在收貨處理天數以內 → 請 IQC 優先檢驗就能趕上，這是企劃救得回來的動作。"""
    r = _run(new_eta="2026-10-19", est=_est(0),
             mat={"gr_processing_days": 3, "gr_source": "光罩料別預設"})
    assert r["gap_days"] == 2
    assert any("IQC" in a and "優先" in a for a in r["actions"])


def test_shortage_beyond_gr_days_does_not_suggest_iqc():
    r = _run(new_eta="2026-10-30", est=_est(0),
             mat={"gr_processing_days": 1, "gr_source": "x"})
    assert not any("IQC" in a for a in r["actions"])


def test_shortage_equal_to_gr_days_does_not_suggest_iqc():
    """
    缺的天數剛好等於收貨處理天數，代表要把檢驗壓縮到 0 天才趕得上——
    這在實務上不可能，不該假裝是「企劃救得回來」的動作
    （回歸：原本用 `<=` 會誤判這種剛好卡滿的單也救得回來）。
    """
    r = _run(new_eta="2026-10-20", est=_est(0),
             mat={"gr_processing_days": 2, "gr_source": "x"})
    assert r["gap_days"] == 2
    assert not any("IQC" in a for a in r["actions"])


def test_shortage_one_day_within_gr_days_suggests_expedited_inspection():
    r = _run(new_eta="2026-10-19", est=_est(0),
             mat={"gr_processing_days": 2, "gr_source": "x"})
    assert r["gap_days"] == 1
    assert any("縮短到 1 天" in a for a in r["actions"])


def test_no_change_with_gr_days_still_shows_a_shortage():
    """
    供應商「確認照原計畫」代表承諾日沒變，不代表一定不缺料——光罩到廠
    要 3 天檢驗＋上線驗證曝光，承諾日離需求日只有 2 天，加上收貨處理
    天數後可投產日還是晚於需求日，一樣要進行動清單，不能因為信件標成
    no_change 就直接丟掉（回歸：原本無條件回「—」）。
    """
    r = evaluate({"change_type": ChangeType.NO_CHANGE.value, "new_eta": None},
                 {**PO, "committed_date": "2026-10-18", "need_date": "2026-10-20"},
                 {**MAT, "gr_processing_days": 3, "gr_source": "光罩料別預設"},
                 CFG, None)
    assert r["priority"] in ("P1", "P2")
    assert r["gap_days"] == 1
    assert any("確認照原計畫" in s and "收貨處理 3 天" in s for s in r["reasons"])
    assert len(r["actions"]) >= 1


def test_no_change_without_gr_impact_still_drops_the_email():
    """gr == 0（或承諾日離需求日夠遠）時，確認不變的信照舊不進行動清單。"""
    r = evaluate({"change_type": ChangeType.NO_CHANGE.value, "new_eta": None},
                 PO, MAT, CFG, _est(9))
    assert r["priority"] == "—" and r["gap_days"] is None
    assert r["actions"] == []


def test_pull_in_with_gr_days_can_still_be_a_shortage():
    """
    提前到的日期，加上收貨處理天數後可能還是晚於需求日——光罩提前一天到，
    但到廠後還要 3 天檢驗＋上線驗證曝光，不能因為「提前」兩字就走進
    倉容那條輕鬆分支，一樣要照斷料分級走
    （回歸：原本只比較收貨當天，忽略了收貨處理天數）。
    """
    r = _run(new_eta="2026-10-19", est=_est(0),
             po={"need_date": "2026-10-20"},
             mat={"gr_processing_days": 3, "gr_source": "光罩料別預設"},
             rec={"change_type": ChangeType.PULL_IN.value})
    assert r["gap_days"] == 2
    assert r["priority"] == "P2"
    assert "供應商提前交貨" not in r["reasons"]
    assert any("供應商已提前到 2026-10-19，但仍晚於下游需求日" in s
              for s in r["reasons"])

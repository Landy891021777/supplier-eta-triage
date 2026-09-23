# -*- coding: utf-8 -*-
"""
企劃確認交期的測試。

守住：確認要有日期與姓名；確認後分級立即改用確認日期；
供應商之後又來新信時，舊的確認不能蓋掉新資訊；永不寫回 ERP。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline  # noqa: E402
import planner_settings as ps  # noqa: E402

TCFG = {"tight_buffer_days": 3, "percentile_confirmed": .8,
        "percentile_estimated": .9, "percentile_intent_only": .95, "min_samples": 20}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "planner_settings.db"


def _row(**kw):
    base = dict(po_no="A", email_id="E1", matched=True, change_type="delay",
                commitment_strength="intent_only", new_eta="2026-10-10",
                committed_date="2026-10-01", need_date="2026-10-20",
                downstream_scheduled=False, has_second_source=True, is_bottleneck=False,
                alt_material_id="", material_id="PR-ArF-1088", category="PHOTORESIST",
                gr_processing_days=0, delay_days_est=0, delay_basis="改期過的單",
                delay_n=40, delay_percentile=.95, estimate_available=True,
                estimate_reason="", needs_human_review=True, supplier_id="SUP-R01",
                received_at=pd.Timestamp("2026-09-08"))
    base.update(kw)
    return base


def test_confirmation_requires_date_and_name(db):
    with pytest.raises(ValueError, match="日期"):
        ps.confirm_eta(db, "A", 1, "E1", "", "電話確認", "王小明")
    with pytest.raises(ValueError, match="姓名"):
        ps.confirm_eta(db, "A", 1, "E1", "2026-10-25", "電話確認", " ")


def test_confirmation_rejects_nan_po_no_and_email_id(db):
    """
    MINOR：po_no／email_id 是 NaN（例如程式帶進來的浮點數，不是使用者
    自己打的字）要拒絕，而且要是看得懂的中文 ValueError，不能讓
    "nan".strip() 這種意外通過驗證，或讓 float 物件呼叫 .strip() 炸出
    英文的 AttributeError。
    """
    with pytest.raises(ValueError, match="採購單號"):
        ps.confirm_eta(db, float("nan"), 1, "E1", "2026-10-30", "", "王小明")
    with pytest.raises(ValueError, match="信件編號"):
        ps.confirm_eta(db, "A", 1, None, "2026-10-30", "", "王小明")


def test_confirmed_date_is_used_and_review_flag_cleared(db):
    ps.confirm_eta(db, "A", 1, "E1", "2026-10-25", "電話確認", "王小明", now="2026-09-22 10:00")
    out = pipeline.retriage(pd.DataFrame([_row()]), {}, TCFG,
                            confirmations=ps.load_confirmations(db))
    r = out.set_index("po_no").loc["A"]
    assert r["new_eta"] == "2026-10-25" and r["commitment_strength"] == "confirmed"
    assert not r["needs_human_review"]
    assert "王小明" in r["reasons"][0] and "尚未寫回 ERP" in r["reasons"][0]
    assert r["gap_days"] == 5          # 10/25 + 延遲估計 0 − 需求日 10/20


def test_newer_supplier_email_supersedes_old_confirmation(db):
    """
    企劃 9/22 依 E1 確認了 10/25；隔天供應商又寄 E2 說要延到 11/10。
    若舊確認蓋掉新信，工具會把一個已經作廢的日期當真。

    舊確認作廢這件事不能默默發生（見 I1）：reasons 第一條要點名是哪個
    舊確認、誰確認的、被哪封新信作廢，企劃才知道「這張單其實已經確認
    過一次」，不是工具漏看了自己的紀錄——但這張單本身仍是「未確認」
    狀態（needs_human_review 仍是 True，commitment_strength 不是
    confirmed），不能因為提到了舊確認就被誤判成已確認。
    """
    ps.confirm_eta(db, "A", 1, "E1", "2026-10-25", "電話確認", "王小明", now="2026-09-22 10:00")
    out = pipeline.retriage(pd.DataFrame([_row(email_id="E2", new_eta="2026-11-10")]), {},
                            TCFG, confirmations=ps.load_confirmations(db))
    r = out.set_index("po_no").loc["A"]
    assert r["new_eta"] == "2026-11-10" and r["needs_human_review"]
    assert r["commitment_strength"] != "confirmed"
    assert ("依 E1 確認的 2026-10-25" in r["reasons"][0]
            and "供應商新信 E2 作廢" in r["reasons"][0]
            and "王小明" in r["reasons"][0])


def test_confirmation_log_keeps_every_entry(db):
    ps.confirm_eta(db, "A", 1, "E1", "2026-10-25", "電話", "王小明", now="2026-09-22 10:00")
    ps.confirm_eta(db, "A", 1, "E1", "2026-10-28", "改口", "王小明", now="2026-09-22 15:00")
    assert [c["confirmed_date"] for c in ps.confirmation_log(db)] == ["2026-10-25", "2026-10-28"]
    assert ps.load_confirmations(db)[("A", 1)]["confirmed_date"] == "2026-10-28"


def test_no_confirmations_changes_nothing():
    df = pd.DataFrame([_row()])
    a = pipeline.retriage(df, {}, TCFG)
    b = pipeline.retriage(df, {}, TCFG, confirmations={})
    assert a[["po_no", "priority", "gap_days"]].equals(b[["po_no", "priority", "gap_days"]])


def test_confirming_a_no_change_row_still_re_derives_change_type(db):
    """
    C1 迴歸測試：供應商信件被解析成「確認照原計畫」（change_type=
    no_change），企劃事後跟供應商要到一個明顯延後的確認日期
    （committed 10/18 → confirm 11/05）。

    _derive_change_type() 原本對 current == no_change 的列直接短路、
    不重判——這條短路是為了 run() 解析信件的路徑設計的（供應商已經明講
    「不變」，不該被巧合解析出的日期蓋掉），但企劃確認交期的路徑上，
    這條短路反而讓「更新的事實」（企劃跟供應商要到的新日期）被「更舊
    的事實」（信件當時說不變）蓋住：change_type 原地不動、gap_days
    沒有跟著重算、needs_human_review 被清掉——最糟的情況下這張單甚至會
    整筆從清單消失（priority 變成「—」）。
    """
    row = _row(change_type="no_change", commitment_strength="confirmed",
              new_eta="2026-10-18", committed_date="2026-10-18",
              need_date="2026-10-20", needs_human_review=False)
    ps.confirm_eta(db, "A", 1, "E1", "2026-11-05", "", "王小明", now="2026-09-22 10:00")
    out = pipeline.retriage(pd.DataFrame([row]), {}, TCFG,
                            confirmations=ps.load_confirmations(db))
    assert "A" in set(out["po_no"]), "列不該因為 no_change 短路而整筆消失"
    r = out.set_index("po_no").loc["A"]
    assert r["change_type"] == "delay"
    assert r["new_eta"] == "2026-11-05"
    assert not r["needs_human_review"]


def test_confirmed_reason_says_confirmed_date_not_supplier_said(db):
    """
    MINOR：確認交期路徑上，理由文字要說「確認交期 {date}」，不能說
    「供應商說 {date}」——日期是企劃自己跟供應商要到、登錄進工具的，
    不是供應商信件裡寫的，用詞要對得上事實來源。
    """
    ps.confirm_eta(db, "A", 1, "E1", "2026-10-25", "", "王小明", now="2026-09-22 10:00")
    out = pipeline.retriage(pd.DataFrame([_row()]), {}, TCFG,
                            confirmations=ps.load_confirmations(db))
    r = out.set_index("po_no").loc["A"]
    joined = "｜".join(r["reasons"])
    assert "確認交期 2026-10-25" in joined
    assert "供應商說 2026-10-25" not in joined


def test_unmatched_row_ignores_confirmation_even_if_po_no_matches(db):
    """
    I3：對不到 PO 主檔的列（matched=False）沒有 committed_date 可比對，
    也沒有 triage.evaluate() 可以重算——即使 confirmations 剛好有同一個
    po_no 的確認紀錄，也不能套用，否則會用一個沒有依據的日期覆寫這一列。
    """
    ps.confirm_eta(db, "X", 1, "E9", "2026-10-30", "", "王小明")
    row = {"po_no": "X", "email_id": "E9", "matched": False, "gap_days": None,
          "priority": "待查", "reasons": ["信中的 PO 號對不到主檔，需人工確認"],
          "actions": [], "needs_human_review": True, "note": "",
          "received_at": pd.Timestamp("2026-09-08")}
    out = pipeline.retriage(pd.DataFrame([row]), {}, TCFG,
                            confirmations=ps.load_confirmations(db))
    r = out.set_index("po_no").loc["X"]
    assert r["needs_human_review"]
    assert r["reasons"] == ["信中的 PO 號對不到主檔，需人工確認"]
    assert "new_eta" not in r or pd.isna(r.get("new_eta"))


def test_confirmation_bound_to_one_batch_does_not_affect_the_other(db):
    """
    分批交貨迴歸測試：同一張單兩批各一列（sched_line 不同），只確認
    第 2 批的交期，第 1 批要完全不受影響——鍵是 (po_no, sched_line)，
    不是單純 po_no，否則一批的確認會誤蓋掉另一批的分級（見決策 19）。
    """
    batch1 = _row(sched_line=1)
    batch2 = _row(sched_line=2, new_eta="2026-11-20", committed_date="2026-11-01")
    ps.confirm_eta(db, "A", 2, "E1", "2026-12-01", "改期", "王小明", now="2026-09-22 10:00")
    out = pipeline.retriage(pd.DataFrame([batch1, batch2]), {}, TCFG,
                            confirmations=ps.load_confirmations(db)).set_index("sched_line")

    r2 = out.loc[2]
    assert r2["new_eta"] == "2026-12-01" and r2["commitment_strength"] == "confirmed"
    assert not r2["needs_human_review"]

    r1 = out.loc[1]
    assert r1["new_eta"] == "2026-10-10"          # _row() 預設值，未被第 2 批的確認蓋掉
    assert r1["commitment_strength"] == "intent_only"
    assert r1["needs_human_review"]


def test_confirming_a_originally_unavailable_row_never_shows_zero_samples(db):
    """
    C2 迴歸測試：一張原本因為沒有新日期而走「待查」早退路徑的單
    （triage.evaluate() 提早 return，delay_n／delay_basis 帶著預設值
    0／""），企劃登錄確認後改用 confirmed 的百分位（delay_days_p80）。

    如果 _select_estimate() 讀的是 row 上被早退污染的 delay_n（0），
    即使 delay_days_p80 明明可用，理由文字還是會說「這家供應商過去
    （0 筆）」——供應商明明有 31 筆歷史，卻說成 0 筆，比不給估計更糟。
    必須改讀 run() 另外存的 delay_n_hist（不受早退路徑影響）。
    """
    row = _row(change_type="unknown", commitment_strength="estimated", new_eta=None,
              delay_days_est=0, delay_basis="", delay_n=0, needs_human_review=True)
    row["delay_days_p80"] = 0.0
    row["delay_days_p90"] = 8.0
    row["delay_days_p95"] = 24.0
    row["delay_n_hist"] = 31
    row["delay_basis_hist"] = "改期過的單"
    row["estimate_available_hist"] = True
    row["estimate_reason_hist"] = ""
    ps.confirm_eta(db, "A", 1, "E1", "2026-10-30", "", "王小明", now="2026-09-22 10:00")
    out = pipeline.retriage(pd.DataFrame([row]), {}, TCFG,
                            confirmations=ps.load_confirmations(db))
    r = out.set_index("po_no").loc["A"]
    joined = "｜".join(r["reasons"])
    assert "（0 筆）" not in joined
    assert "（31 筆）" in joined

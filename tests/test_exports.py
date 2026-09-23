# -*- coding: utf-8 -*-
"""匯出與月度績效的測試：企劃拿去開會的東西，數字與欄位不能錯。"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import exports  # noqa: E402


def _actions():
    common = dict(material_id="PR-ArF-1088", category="PHOTORESIST", supplier_name="Resist-Echo",
                  qty=80, base_uom="GAL", new_eta="2026-10-10", conservative_eta="2026-10-20",
                  available_date="2026-10-22", need_date="2026-10-15",
                  commitment_strength="estimated", needs_human_review=True)
    return pd.DataFrame([
        {**common, "priority": "P1", "gap_days": 7.0, "po_no": "A",
         "reasons": ["r1", "r2"], "actions": ["a1"]},
        {**common, "priority": "P3", "gap_days": -9.0, "po_no": "B",
         "reasons": ["r"], "actions": []},
        {**common, "priority": "待查", "gap_days": float("nan"), "po_no": "C",
         "reasons": ["讀不出"], "actions": []},
    ])


def test_followup_workbook_keeps_p1_p2_and_pending_only():
    wb = load_workbook(io.BytesIO(exports.followup_workbook(_actions(), "2026-09-08")))
    ws = wb["追料清單"]
    header = [c.value for c in ws[1]]
    assert header[:4] == ["優先級", "預估缺料天數", "採購單號", "料號"]
    pos = [r[header.index("採購單號")] for r in ws.iter_rows(min_row=2, values_only=True)]
    assert pos == ["A", "C"]
    assert "說明" in wb.sheetnames


def test_followup_workbook_writes_integers_and_joined_text():
    """Excel 裡出現 7.0、nan 或 ['a1'] 這種字，企劃拿去開會會被笑。"""
    wb = load_workbook(io.BytesIO(exports.followup_workbook(_actions(), "2026-09-08")))
    ws = wb["追料清單"]
    header = [c.value for c in ws[1]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert rows[0][header.index("預估缺料天數")] == 7
    assert rows[1][header.index("預估缺料天數")] in (None, "")
    assert rows[0][header.index("建議動作")] == "a1"
    assert rows[0][header.index("數量")] == "80 GAL"


def test_followup_workbook_shows_commitment_strength_in_chinese():
    """承諾強度存的是 estimated/confirmed 這種代碼；企劃看報表要看得懂中文，
    不是工程用的英文代碼。"""
    wb = load_workbook(io.BytesIO(exports.followup_workbook(_actions(), "2026-09-08")))
    ws = wb["追料清單"]
    header = [c.value for c in ws[1]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert rows[0][header.index("承諾強度")] == "暫估"


def _outcomes():
    rows = []
    for i in range(6):   # 2026-05：6 筆，2 筆延遲
        rows.append(dict(supplier_id="S1", committed_date=f"2026-05-{i+1:02d}",
                         delay_days=5 if i < 2 else 0, reschedule_count=1 if i < 3 else 0))
    for i in range(3):   # 2026-06：3 筆 → 樣本不足
        rows.append(dict(supplier_id="S1", committed_date=f"2026-06-{i+1:02d}",
                         delay_days=0, reschedule_count=0))
    return pd.DataFrame(rows)


def test_monthly_performance_counts_and_rates():
    m = exports.supplier_monthly(_outcomes()).set_index(["供應商", "承諾月份"])
    may = m.loc[("S1", "2026-05")]
    assert may["交貨筆數"] == 6
    assert abs(may["準交率"] - 4 / 6) < 1e-9
    assert abs(may["改期比例"] - 0.5) < 1e-9
    assert may["延遲時中位數(天)"] == 5


def test_monthly_performance_refuses_small_samples():
    """只有 3 筆的月份算出「準交率 100%」會誤導評核，寧可標樣本不足。"""
    jun = exports.supplier_monthly(_outcomes()).set_index(["供應商", "承諾月份"]).loc[("S1", "2026-06")]
    assert jun["樣本"] == "樣本不足" and pd.isna(jun["準交率"])


def test_followup_workbook_has_committed_date_as_real_excel_dates():
    """
    MINOR：原承諾日欄要在新交期之後；日期欄要寫成真正的 Excel 日期
    （openpyxl 讀回來是 datetime，不是文字字串），企劃才能在 Excel 裡
    排序、篩選、做樞紐分析，不必自己重新剖析文字日期。
    """
    df = _actions()
    df["committed_date"] = ["2026-09-20", "2026-09-01", "2026-09-25"]
    wb = load_workbook(io.BytesIO(exports.followup_workbook(df, "2026-09-08")))
    ws = wb["追料清單"]
    header = [c.value for c in ws[1]]
    assert header.index("原承諾日") == header.index("新交期") + 1
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    import datetime as _dt
    assert isinstance(rows[0][header.index("新交期")], _dt.datetime)
    assert rows[0][header.index("原承諾日")] == _dt.datetime(2026, 9, 20)


def test_followup_workbook_pending_row_gets_default_action_text():
    """
    MINOR：待查的單常常沒有建議動作（triage 早退），但「什麼都沒寫」
    會讓企劃以為這張單不用管——待查比 P1/P2 更需要人去看一眼原信。
    """
    wb = load_workbook(io.BytesIO(exports.followup_workbook(_actions(), "2026-09-08")))
    ws = wb["追料清單"]
    header = [c.value for c in ws[1]]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    # _actions() 的第三筆（po_no C）是「待查」、actions=[]
    tq = [r for r in rows if r[header.index("採購單號")] == "C"][0]
    assert tq[header.index("建議動作")] == "請人工看原信，確認單號與新交期"


def test_followup_workbook_explanation_sheet_has_no_english_jargon():
    """MINOR：「模擬基準日（as_of）」的英文縮寫企劃看不懂，拿掉。"""
    wb = load_workbook(io.BytesIO(exports.followup_workbook(_actions(), "2026-09-08")))
    notes = [c.value for c in wb["說明"]["A"]]
    assert any(n and n.startswith("模擬基準日：") for n in notes)
    assert not any(n and "as_of" in n for n in notes)


def _open_pos():
    # 7 筆在途、承諾日都已過 as_of（2026-08-01），加上 6/1、6/15 兩筆
    # 未逾期的，湊出一個樣本數 >= MIN_MONTHLY_SAMPLES 的月份，才能驗證
    # 「準交率」不是被小樣本規則 NaN 掉，而是真的把逾期單算成延遲。
    rows = [dict(supplier_id="S1", committed_date=f"2026-07-{i:02d}", reschedule_count=0)
            for i in range(1, 6)]
    rows.append(dict(supplier_id="S1", committed_date="2026-08-20", reschedule_count=0))
    return pd.DataFrame(rows)


def test_monthly_performance_counts_overdue_open_pos_as_late():
    """
    I6：承諾日已過、還沒收貨的在途單，要算進「逾期未收（筆）」，並且當作
    一筆延遲（delay_days 用到基準日為止的天數，低估）計入準交率／
    交貨筆數，不能因為還沒收貨就完全不出現在統計裡——那會系統性
    高估最近月份的準交率。
    """
    mon = exports.supplier_monthly(_outcomes(), open_pos=_open_pos(), as_of="2026-08-01")
    m = mon.set_index(["供應商", "承諾月份"])
    jul = m.loc[("S1", "2026-07")]
    assert jul["逾期未收（筆）"] == 5
    assert jul["交貨筆數"] == 5  # 這個月本來沒有已收貨紀錄，全部來自在途逾期單
    assert jul["樣本"] == "足夠"
    assert jul["準交率"] == 0.0  # 5 筆全逾期未收，視為延遲
    aug = m.loc[("S1", "2026-08")] if ("S1", "2026-08") in m.index else None
    assert aug is None or aug["逾期未收（筆）"] == 0  # 8/20 尚未過 as_of 8/1，不算逾期


def test_monthly_performance_without_open_pos_has_zero_overdue_column():
    """沒給 open_pos／as_of 時完全比照舊行為，只是多一欄全 0 的「逾期未收」。"""
    mon = exports.supplier_monthly(_outcomes())
    assert (mon["逾期未收（筆）"] == 0).all()


def test_monthly_performance_supplier_name_is_first_column():
    """MINOR：企劃看名字，不是看供應商代號，名稱欄放最前面。"""
    outc = _outcomes()
    outc["supplier_name"] = "泓格光阻"
    mon = exports.supplier_monthly(outc)
    assert mon.columns[0] == "供應商名稱"
    assert mon.columns[1] == "供應商"

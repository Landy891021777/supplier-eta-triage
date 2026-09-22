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

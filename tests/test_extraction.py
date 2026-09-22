# -*- coding: utf-8 -*-
"""
測試重點放在「錯了會出事」的行為，不是追求覆蓋率數字。

這個工具的失敗模式有輕重之分：
    可接受   — 看不懂一封信，標記為需人工確認
    不可接受 — 把託辭當成承諾、把「確認不變」當成延遲、抓到轉寄串裡的舊日期

下面的測試優先守住第二類。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import extract_rules  # noqa: E402
from domain import ChangeType  # noqa: E402
from handcrafted_emails import HANDCRAFTED  # noqa: E402

HC = {e["email_id"]: e for e in HANDCRAFTED}


def _extract(email_id: str):
    e = HC[email_id]
    return [r.to_dict() for r in extract_rules.extract(
        {"subject": e["subject"], "body": e["body"], "supplier_id": e["supplier_id"]})]


# ---------------------------------------------------------------------------
# PO 辨識
# ---------------------------------------------------------------------------
def test_finds_all_pos_in_multi_po_email():
    """一封信講兩張單時，兩張都要抓到 —— 漏掉的那張就沒人管了。"""
    pos = {r["po_no"] for r in _extract("HC-001")}
    assert pos == {"PO-2026-04417", "PO-2026-04452"}


def test_finds_three_pos():
    pos = {r["po_no"] for r in _extract("HC-008")}
    assert pos == {"PO-2026-04277", "PO-2026-04278", "PO-2026-04279"}


# ---------------------------------------------------------------------------
# 「確認不變」不可以被當成延遲（否則行動清單會被雜訊灌爆）
# ---------------------------------------------------------------------------
def test_no_change_email_is_not_treated_as_delay():
    recs = _extract("HC-006")
    assert len(recs) == 1
    assert recs[0]["change_type"] == ChangeType.NO_CHANGE.value
    assert recs[0]["new_eta"] is None, "確認不變時不應輸出新交期"


# ---------------------------------------------------------------------------
# 規則層必須知道自己什麼時候不該逞強
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("email_id", ["HC-001", "HC-005", "HC-008"])
def test_low_confidence_on_vague_or_relative_dates(email_id):
    """含相對日期／模糊措辭的信，規則層信心必須低到會觸發升級。"""
    conf = max(r["confidence"] for r in _extract(email_id))
    assert conf < 0.75, f"{email_id} 信心過高，會錯失升級到 LLM 的機會"


def test_low_confidence_on_forwarded_chain():
    """轉寄串無法分辨新舊資訊，必須降低信心。"""
    conf = max(r["confidence"] for r in _extract("HC-003"))
    assert conf <= 0.45


def test_high_confidence_on_formal_table():
    """反過來說，格式化通知信要有高信心，否則會白白花錢呼叫 LLM。"""
    conf = max(r["confidence"] for r in _extract("HC-002"))
    assert conf >= 0.75


# ---------------------------------------------------------------------------
# 日期格式
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("Revised ETA: 2026-10-09", date(2026, 10, 9)),
    ("delivery moved to 30-Sep-2026", date(2026, 9, 30)),
    ("順延至 9 月 22 日", date(2026, 9, 22)),
    ("New target is around Oct 20", date(2026, 10, 20)),
])
def test_date_formats(text, expected):
    found = extract_rules._find_dates(text, 2026)
    assert expected in [d for d, _s, _e, _raw in found], f"未解析出 {expected}：{text}"


def test_po_normalisation():
    assert extract_rules._norm_po("po 2026 04417") == "PO-2026-04417"
    assert extract_rules._norm_po("PO-2026-04417") == "PO-2026-04417"


def test_original_label_does_not_poison_next_line():
    """
    回歸測試。

    表格式通知的兩行標籤緊鄰：
        Original ETA : 2026-09-25
        Revised ETA  : 2026-10-09
    早期版本只判斷「左側視窗有沒有 Original」，導致第二個日期被上一行的
    標籤污染，整封信被誤判並白白升級呼叫 LLM。
    """
    text = "Original ETA : 2026-09-25\nRevised ETA  : 2026-10-09\n"
    pos = text.index("2026-10-09")
    assert extract_rules._nearest_label(text, pos) == "new"

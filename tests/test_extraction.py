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


# ---------------------------------------------------------------------------
# 分批交貨：一封信可以講同一張單的多批交期
# ---------------------------------------------------------------------------
def test_hc010_split_shipment_yields_two_records():
    """
    HC-010：「8 pcs on the original date 2026-09-30, remaining 12 pcs
    deferred to 2026-11-15」必須拆成兩筆，不能只取最晚那批——
    只取最晚一筆等於把準時到的 8 片當成不存在，缺料清單會漏看真正的風險。
    """
    recs = _extract("HC-010")
    assert len(recs) == 2
    by_qty = {r["qty"]: r for r in recs}
    assert set(by_qty) == {8, 12}

    on_time = by_qty[8]
    assert on_time["po_no"] == "PO-2026-04188"
    assert on_time["new_eta"] == "2026-09-30"
    assert on_time["change_type"] == ChangeType.NO_CHANGE.value

    delayed = by_qty[12]
    assert delayed["po_no"] == "PO-2026-04188"
    assert delayed["new_eta"] == "2026-11-15"
    assert delayed["change_type"] == ChangeType.DELAY.value


def test_split_shipment_generalises_to_different_wording():
    """
    規則不可以寫死 HC-010 的句子——換一種說法（不同單位、不同日期格式）
    也要抓得到兩批，否則只是背答案，遇到真實信件的其他寫法就會失效。
    """
    email = {
        "subject": "Shipment update",
        "body": ("PO-2026-09999: 1,000 pcs ship on 10/05, the remaining 500 pcs "
                 "will follow on 10/26."),
        "supplier_id": "SUP-TEST",
    }
    recs = [r.to_dict() for r in extract_rules.extract(email)]
    assert len(recs) == 2
    by_qty = {r["qty"]: r for r in recs}
    assert set(by_qty) == {1000, 500}
    assert by_qty[1000]["new_eta"] == "2026-10-05"
    assert by_qty[1000]["change_type"] == ChangeType.NO_CHANGE.value
    assert by_qty[500]["new_eta"] == "2026-10-26"
    assert by_qty[500]["change_type"] == ChangeType.DELAY.value


def test_split_shipment_chinese_wording_without_first_batch_date():
    """
    中文寫法「先出 X，其餘 Y 延到某日」通常不會重述第一批的日期——
    這時第一批的 new_eta 應為 null（不可亂猜），但仍要拆成兩筆、各帶 qty。
    """
    email = {
        "subject": "分批出貨通知",
        "body": "PO-2026-08888 先出 2,000 片，其餘 3,000 片延到 11/15",
        "supplier_id": "SUP-TEST",
    }
    recs = [r.to_dict() for r in extract_rules.extract(email)]
    assert len(recs) == 2
    by_qty = {r["qty"]: r for r in recs}
    assert by_qty[2000]["new_eta"] is None
    assert by_qty[2000]["change_type"] == ChangeType.NO_CHANGE.value
    assert by_qty[3000]["new_eta"] == "2026-11-15"
    assert by_qty[3000]["change_type"] == ChangeType.DELAY.value


def test_non_split_email_still_yields_one_record():
    """
    回歸測試：沒有分批的信不可以被誤判成分批，否則好端端一張單會被拆成兩筆。
    HC-006 是單張單的「確認不變」信，沒有「其餘／remaining」這類轉折詞。
    """
    recs = _extract("HC-006")
    assert len(recs) == 1


def test_score_email_scores_split_batches_separately():
    """
    分批交貨的兩批要能分別計分：其中一批日期抓對、另一批抓錯，
    eta_exact 應該是 0.5 而不是被同一個 po_no 蓋成一筆。
    """
    import evaluate
    truth = [
        {"po_no": "PO-2026-04188", "qty": 8, "new_eta": "2026-09-30",
         "commitment_strength": "confirmed", "change_type": "no_change"},
        {"po_no": "PO-2026-04188", "qty": 12, "new_eta": "2026-11-15",
         "commitment_strength": "confirmed", "change_type": "delay"},
    ]
    pred = [
        {"po_no": "PO-2026-04188", "qty": 8, "new_eta": "2026-09-30",
         "commitment_strength": "confirmed", "change_type": "no_change"},
        {"po_no": "PO-2026-04188", "qty": 12, "new_eta": "2026-10-01",  # 日期抓錯
         "commitment_strength": "estimated", "change_type": "delay"},
    ]
    s = evaluate.score_email(pred, truth)
    assert s["po_hit"] == 1.0, "兩批都有輸出，即使其中一批日期錯，po_hit 仍應為 1"
    assert s["eta_exact"] == 0.5


def test_score_email_counts_false_confirmed_separately():
    """
    把託辭判成「已確認」是本工具最危險的錯，必須單獨算出來。
    只看 strength_ok 的話，它跟「暫估判成僅意向」這種無害的錯被算成一樣。
    """
    import evaluate
    truth = [{"po_no": "A", "commitment_strength": "intent_only", "change_type": "delay",
              "new_eta": "2026-10-31"},
             {"po_no": "B", "commitment_strength": "estimated", "change_type": "delay",
              "new_eta": "2026-10-20"}]
    pred = [{"po_no": "A", "commitment_strength": "confirmed", "change_type": "delay",
             "new_eta": "2026-10-31"},
            {"po_no": "B", "commitment_strength": "intent_only", "change_type": "delay",
             "new_eta": "2026-10-20"}]
    s = evaluate.score_email(pred, truth)
    assert s["strength_ok"] == 0.0
    assert s["false_confirmed"] == 1

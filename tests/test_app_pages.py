# -*- coding: utf-8 -*-
"""
每一頁都要能在離線、沒有金鑰的情況下打開。

頁面拆成檔案後，最常見的壞法是某頁少 import 一個東西，只有點到那頁才會爆。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "views").glob("*.py")
               if p.name != "__init__.py")


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    import planner_settings
    monkeypatch.setattr(planner_settings, "DEFAULT_DB", tmp_path / "ps.db")


def test_home_page_runs():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    assert not at.exception, at.exception


@pytest.mark.parametrize("page", PAGES)
def test_every_page_runs(page):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    at.switch_page(page).run()
    assert not at.exception, (page, at.exception)


# ---------------------------------------------------------------------------
# Task 4：企劃確認交期表單、兩個匯出按鈕、供應商月度績效
# ---------------------------------------------------------------------------
def _first_needs_review_row():
    """
    直接用底層函式（不透過 Streamlit）找一張目前 needs_human_review 的單，
    跟頁面上看到的資料是同一份（沒有覆寫、沒有確認的乾淨狀態）。
    比起在畫面上用 head(12) 亂猜哪張單會被展開，這樣才能保證測試找到的
    po_no／email_id 是真的存在、而且跟表單 key 用的是同一組值。
    """
    import pipeline as _pipeline
    result = _pipeline.run(use_llm=False)
    tcfg = _pipeline.load_config()["triage"]
    actions = _pipeline.retriage(result.get("all", result["actions"]), {}, tcfg)
    row = actions[actions["needs_human_review"]].iloc[0]
    return row["po_no"], row["email_id"]


def test_confirmation_form_clears_review_flag_and_logs(tmp_path):
    """
    守住：企劃在畫面上填表單送出後，rerun 一次分級就要立即改用確認日期
    （不再是需人工確認），而且確認紀錄真的寫進資料庫——不能表單顯示
    「已登錄」，資料庫卻是空的。
    """
    from datetime import date

    po_no, email_id = _first_needs_review_row()

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    at.switch_page("views/actions.py").run()

    # 目標單不一定落在預設篩選（P1、P2）裡：展開所有優先級＋只看需人工確認，
    # 才能保證這張單的展開區（含表單）真的被渲染出來。
    at.multiselect(key="action_priority_pick").set_value(["P1", "P2", "P3", "待查"])
    at.checkbox(key="action_only_review").set_value(True)
    at.run()

    at.get_by_key(f"confirm_date_{po_no}_{email_id}").set_value(date(2026, 10, 1))
    at.get_by_key(f"confirm_user_{po_no}_{email_id}").set_value("王小明")
    at.get_by_key(f"confirm_submit_{po_no}_{email_id}").click()
    at.run()
    assert not at.exception, at.exception

    import planner_settings as ps
    log = ps.confirmation_log()
    assert len(log) == 1
    assert log[0]["po_no"] == po_no and log[0]["confirmed_by"] == "王小明"
    assert log[0]["confirmed_date"] == "2026-10-01"

    # 確認後這張單不再需人工確認：因為 only_review 勾著，它現在應該從
    # 「只看需人工確認」的篩選結果裡消失（表單／按鈕都不該再出現）。
    at2 = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    at2.switch_page("views/actions.py").run()
    at2.multiselect(key="action_priority_pick").set_value(["P1", "P2", "P3", "待查"])
    at2.checkbox(key="action_only_review").set_value(True)
    at2.run()
    assert not at2.exception, at2.exception
    remaining_keys = {b.key for b in at2.button}
    assert f"confirm_submit_{po_no}_{email_id}" not in remaining_keys


def test_action_page_has_both_download_buttons_and_excel_is_readable():
    """
    守住：CSV 匯出（操作指引已提到，文字不能改）與新的 Excel 匯出都要在，
    且 Excel 真的讀得回來（欄位對得上，不是壞檔）。
    """
    import io

    from openpyxl import load_workbook

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    at.switch_page("views/actions.py").run()
    assert not at.exception, at.exception

    labels = {b.label for b in at.download_button}
    assert "⬇️ 匯出行動清單 CSV" in labels
    assert "⬇️ 匯出明日追料清單（Excel）" in labels

    import exports as _exports
    import pipeline as _pipeline

    result = _pipeline.run(use_llm=False)
    tcfg = _pipeline.load_config()["triage"]
    actions = _pipeline.retriage(result.get("all", result["actions"]), {}, tcfg)
    wb = load_workbook(io.BytesIO(_exports.followup_workbook(actions, str(result["as_of"]))))
    assert "追料清單" in wb.sheetnames and "說明" in wb.sheetnames


def test_supplier_page_runs_and_month_select_has_options():
    """守住：月度績效的月份選單要有選項可選，不能是空的下拉。"""
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300).run()
    at.switch_page("views/suppliers.py").run()
    assert not at.exception, at.exception

    month_boxes = [s for s in at.selectbox if s.label == "月份"]
    assert month_boxes and len(month_boxes[0].options) > 0

    dl_labels = {b.label for b in at.download_button}
    assert "⬇️ 匯出供應商月度績效（Excel）" in dl_labels

# -*- coding: utf-8 -*-
"""
歷史單據產生器的測試。

全部在資料庫的「副本」上執行，不會動到 data/erp_sim.db。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DB = ROOT / "data" / "erp_sim.db"
pytestmark = pytest.mark.skipif(
    not DB.exists(), reason="需先執行 py src/build_erp_db.py")


@pytest.fixture(scope="module")
def rebuilt(tmp_path_factory):
    import generate_history
    dst = tmp_path_factory.mktemp("erp") / "erp_copy.db"
    shutil.copy(DB, dst)
    generate_history.build_history(verbose=False, db_path=dst)
    return dst


def _late_rate(con, vendor: str, lo: str, hi: str) -> tuple[int, float]:
    n, late = con.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN g.receipt_date > s.committed_date THEN 1 ELSE 0 END)
        FROM goods_receipt g
        JOIN po_header   h ON h.po_no = g.po_no
        JOIN po_schedule s ON s.po_no = g.po_no AND s.item_no = g.item_no
        WHERE h.vendor_id = ? AND s.committed_date >= ? AND s.committed_date < ?
    """, (vendor, lo, hi)).fetchone()
    return n, (late or 0) / n if n else float("nan")


def test_degrading_supplier_gets_worse_over_time(rebuilt):
    """
    SUP-S02 從 2026-03-01 起表現變差。

    若產生器整年都是固定分布，時間切分回測的涵蓋率必然接近設定值，
    「驗證」就成了自己驗自己。這個測試守住「表現真的會隨時間變」。
    """
    with sqlite3.connect(rebuilt) as con:
        n_before, before = _late_rate(con, "SUP-S02", "2025-01-01", "2026-03-01")
        n_after, after = _late_rate(con, "SUP-S02", "2026-03-01", "2027-01-01")
    assert min(n_before, n_after) >= 15, (n_before, n_after)
    assert after - before >= 0.15, (before, after)


def test_improving_supplier_gets_better_over_time(rebuilt):
    with sqlite3.connect(rebuilt) as con:
        n_before, before = _late_rate(con, "SUP-F03", "2025-01-01", "2026-04-01")
        n_after, after = _late_rate(con, "SUP-F03", "2026-04-01", "2027-01-01")
    assert min(n_before, n_after) >= 15, (n_before, n_after)
    assert before - after >= 0.15, (before, after)


def test_generator_is_idempotent(rebuilt):
    """
    回歸測試：歷史產生器必須冪等，跑一次跟跑十次結果要一樣。

    早期版本直接 INSERT 變更文件，重跑一次就寫入第二遍，
    po_change_log 從 904 筆變成 1599 筆，改期次數憑空翻倍。
    這種 bug 不會報錯，資料照樣跑得出來，只是悄悄地錯。
    """
    import generate_history

    def snapshot():
        with sqlite3.connect(rebuilt) as con:
            return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("po_header", "po_item", "po_schedule",
                              "po_change_log", "goods_receipt", "purchase_req")}

    first = snapshot()
    generate_history.build_history(verbose=False, db_path=rebuilt)
    assert snapshot() == first


def test_no_duplicate_change_log_rows(rebuilt):
    """變更文件不可有內容完全相同的重複列。"""
    with sqlite3.connect(rebuilt) as con:
        total = con.execute("SELECT COUNT(*) FROM po_change_log").fetchone()[0]
        distinct = con.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT po_no, item_no, old_value,"
            " new_value, changed_at FROM po_change_log)").fetchone()[0]
    assert total == distinct, f"變更文件有 {total - distinct} 筆重複"

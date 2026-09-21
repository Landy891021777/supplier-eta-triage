# -*- coding: utf-8 -*-
"""
歷史單據產生器的測試。

全部在資料庫的「副本」上執行，不會動到 data/erp_sim.db。
"""
from __future__ import annotations

import random
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
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


def _outcomes(vendor: str, committed: date, n: int = 20000) -> list[int]:
    """固定其他條件，只換承諾日，抽 n 次「實際到料日 − 承諾日」。"""
    import generate_history
    rng = random.Random(0)
    return [generate_history._simulate_outcome(
        rng, vendor_id=vendor, otd_rate=0.85, category="WAFER",
        is_bottleneck=False, reschedule_count=0, committed=committed, qty=1000)
        for _ in range(n)]


def _late_stats(deltas: list[int]) -> tuple[float, float]:
    """回傳（延遲比例，延遲單的平均天數）。"""
    late = [d for d in deltas if d > 0]
    return len(late) / len(deltas), sum(late) / len(late)


# 領域假設：日期選在月初 5 號，離季末（最後 14 天）超過兩週，
# 避免 _is_quarter_end 的 +0.10 混進「漂移前後」的比較。
S02_BEFORE, S02_AFTER = date(2026, 2, 5), date(2026, 3, 5)
F03_BEFORE, F03_AFTER = date(2026, 3, 5), date(2026, 4, 5)


def test_drift_dates_are_outside_quarter_end_window():
    """
    回歸守門：測試日期若落進季末窗口，季末的 +0.10 會讓「漂移前後」
    的差被別的因素污染，測試結果就不再代表漂移本身。
    """
    import generate_history
    for d in (S02_BEFORE, S02_AFTER, F03_BEFORE, F03_AFTER):
        assert not generate_history._is_quarter_end(d), d
    assert S02_BEFORE < generate_history.SUPPLIER_DRIFT["SUP-S02"]["from"] <= S02_AFTER
    assert F03_BEFORE < generate_history.SUPPLIER_DRIFT["SUP-F03"]["from"] <= F03_AFTER


def test_degrading_supplier_drift_is_real():
    """
    SUP-S02 從 2026-03-01 起延遲機率上升、延遲天數變長。

    刻意與種子無關：直接呼叫 _simulate_outcome 抽兩萬次，只換承諾日。
    原本的資料庫層測試每邊只有 35～40 張單，200 個種子中約 21% 的變化量
    掉到 0.15 以下，任何無關的產生器改動（N_HISTORY、多一次 rng 呼叫）
    都可能讓它無故變紅。真正的漂移斷言放在這裡。
    """
    before, before_days = _late_stats(_outcomes("SUP-S02", S02_BEFORE))
    after, after_days = _late_stats(_outcomes("SUP-S02", S02_AFTER))
    print(f"S02 late fraction {before:.3f} -> {after:.3f}; "
          f"mean late days {before_days:.2f} -> {after_days:.2f}")
    assert after - before >= 0.15, (before, after)
    assert after_days > before_days, (before_days, after_days)


def test_improving_supplier_drift_is_real():
    """
    SUP-F03 從 2026-04-01 起延遲機率下降、延遲天數變短。

    刻意與種子無關，原因同 test_degrading_supplier_drift_is_real：
    小樣本的資料庫層比較太吵，不能拿來守漂移量。
    """
    before, before_days = _late_stats(_outcomes("SUP-F03", F03_BEFORE))
    after, after_days = _late_stats(_outcomes("SUP-F03", F03_AFTER))
    print(f"F03 late fraction {before:.3f} -> {after:.3f}; "
          f"mean late days {before_days:.2f} -> {after_days:.2f}")
    assert before - after >= 0.15, (before, after)
    assert after_days < before_days, (before_days, after_days)


def test_degrading_supplier_gets_worse_in_generated_db(rebuilt):
    """
    SUP-S02 在產生出的資料庫裡，2026-03-01 之後的延遲率要比之前高。

    這只是方向的健全性檢查，樣本每邊僅約 35～40 張。若在無關的產生器
    改動後變紅，代表種子或樣本數變了，不代表漂移壞掉；
    真正的漂移量斷言在 test_degrading_supplier_drift_is_real（與種子無關）。
    """
    with closing(sqlite3.connect(rebuilt)) as con:
        n_before, before = _late_rate(con, "SUP-S02", "2025-01-01", "2026-03-01")
        n_after, after = _late_rate(con, "SUP-S02", "2026-03-01", "2027-01-01")
    assert min(n_before, n_after) >= 15, (n_before, n_after)
    assert after > before, (before, after)


def test_improving_supplier_gets_better_in_generated_db(rebuilt):
    """
    SUP-F03 在產生出的資料庫裡，2026-04-01 之後的延遲率要比之前低。

    同上，只檢查方向；漂移量的斷言在 test_improving_supplier_drift_is_real。
    """
    with closing(sqlite3.connect(rebuilt)) as con:
        n_before, before = _late_rate(con, "SUP-F03", "2025-01-01", "2026-04-01")
        n_after, after = _late_rate(con, "SUP-F03", "2026-04-01", "2027-01-01")
    assert min(n_before, n_after) >= 15, (n_before, n_after)
    assert before > after, (before, after)


def test_generator_is_idempotent(rebuilt):
    """
    回歸測試：歷史產生器必須冪等，跑一次跟跑十次結果要一樣。

    早期版本直接 INSERT 變更文件，重跑一次就寫入第二遍，
    po_change_log 從 904 筆變成 1599 筆，改期次數憑空翻倍。
    這種 bug 不會報錯，資料照樣跑得出來，只是悄悄地錯。
    """
    import generate_history

    def snapshot():
        with closing(sqlite3.connect(rebuilt)) as con:
            snap = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("po_header", "po_item", "po_schedule",
                              "po_change_log", "goods_receipt", "purchase_req")}
            # 筆數相同但內容變了也是不冪等（例如亂數序列被打亂），所以加內容雜湊
            snap["receipt_checksum"] = con.execute(
                "SELECT group_concat(gr_no || receipt_date, ',') FROM "
                "(SELECT gr_no, receipt_date FROM goods_receipt ORDER BY gr_no)"
            ).fetchone()[0]
            return snap

    first = snapshot()
    generate_history.build_history(verbose=False, db_path=rebuilt)
    assert snapshot() == first


def test_no_duplicate_change_log_rows(rebuilt):
    """變更文件不可有內容完全相同的重複列。"""
    with closing(sqlite3.connect(rebuilt)) as con:
        total = con.execute("SELECT COUNT(*) FROM po_change_log").fetchone()[0]
        distinct = con.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT po_no, item_no, old_value,"
            " new_value, changed_at FROM po_change_log)").fetchone()[0]
    assert total == distinct, f"變更文件有 {total - distinct} 筆重複"

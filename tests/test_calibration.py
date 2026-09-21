# -*- coding: utf-8 -*-
"""
權重校準的測試。

守住的重點只有一個：**不可以資料洩漏。**

校準最容易犯、也最難察覺的錯，是把「後來才知道的事」當成特徵。
一旦實際到料日混進特徵裡，AUC 會漂亮得不像話，而模型完全沒用 ——
因為上線時你根本沒有那個欄位。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DB = ROOT / "data" / "erp_sim.db"
pytestmark = pytest.mark.skipif(
    not DB.exists(),
    reason="需先執行 py src/build_erp_db.py 與 py src/generate_history.py")


@pytest.fixture(scope="module")
def history():
    import calibrate
    return calibrate.load_history()


def test_history_has_outcomes(history):
    assert len(history) > 100, "歷史樣本太少，校準沒有意義"
    assert history["receipt_date"].notna().all()


def test_features_exclude_future_information(history):
    """
    特徵裡絕對不可以出現實際到料日。

    這是校準最容易犯、也最難察覺的錯：一旦洩漏，AUC 會漂亮得不像話，
    但上線時根本沒有那個欄位可用。
    """
    import calibrate
    X, y = calibrate.build_features(history)
    leaked = [c for c in X.columns
              if "receipt" in c.lower() or "actual" in c.lower()]
    assert leaked == [], f"特徵中含有未來資訊：{leaked}"
    assert set(y.unique()) <= {0, 1}
    assert 0 < y.mean() < 1, "outcome 全為同一類，無法校準"


def test_commitment_strength_is_excluded(history):
    """
    承諾強度來自信件語氣，ERP 沒有這個欄位，不可能用歷史資料校準。
    把它留在特徵裡會讓人誤以為它被驗證過。
    """
    import calibrate
    X, _ = calibrate.build_features(history)
    assert "commitment_strength" not in X.columns
    assert "commitment_strength" in calibrate.UNCALIBRATABLE


def test_all_rule_scores_in_valid_range(history):
    import calibrate
    X, _ = calibrate.build_features(history)
    assert X.min().min() >= 0.0
    assert X.max().max() <= 1.0


def test_calibration_output_shape():
    import calibrate
    res = calibrate.calibrate()
    t = res["table"]
    assert {"人訂權重", "學到的係數", "校準後權重", "資料的意見"} <= set(t.columns)
    # 校準後的權重不可為負：負係數代表「資料不支持」，
    # 正確處理是截為 0 並標示分歧，而不是塞一個負權重進評分卡。
    assert (t["校準後權重"] >= 0).all()
    assert 0.0 <= res["auc_hand"] <= 1.0
    assert 0.0 <= res["auc_model"] <= 1.0


def test_auc_is_not_suspiciously_perfect():
    """
    AUC 接近 1.0 幾乎一定是資料洩漏，不是模型很強。
    這個測試是刻意設計的「太好了反而不對」防呆。
    """
    import calibrate
    res = calibrate.calibrate()
    assert res["auc_model"] < 0.95, (
        f"AUC {res['auc_model']:.3f} 高得可疑，請檢查是否有未來資訊洩漏")


# ---------------------------------------------------------------------------
# 冪等性（回歸測試）
# ---------------------------------------------------------------------------
def test_history_generation_is_idempotent():
    """
    回歸測試：歷史產生器必須冪等，跑一次跟跑十次結果要一樣。

    早期版本直接 INSERT 變更文件，重跑一次就寫入第二遍，
    po_change_log 從 904 筆變成 1599 筆，改期次數憑空翻倍，
    權重校準的結果因此每跑一次都不同。

    這種 bug 不會報錯、資料照樣跑得出來，只是悄悄地錯 ——
    而且直接違反專案標榜的「可重現」。
    """
    import sqlite3

    import generate_history

    def snapshot():
        with sqlite3.connect(generate_history.DB_PATH) as con:
            return {
                t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("po_header", "po_item", "po_schedule",
                          "po_change_log", "goods_receipt", "purchase_req")
            }

    generate_history.build_history(verbose=False)
    first = snapshot()
    generate_history.build_history(verbose=False)
    second = snapshot()
    assert first == second, f"重跑後筆數改變：{first} -> {second}"


def test_no_duplicate_change_log_rows():
    """變更文件不可有內容完全相同的重複列。"""
    import sqlite3

    import generate_history

    with sqlite3.connect(generate_history.DB_PATH) as con:
        total = con.execute("SELECT COUNT(*) FROM po_change_log").fetchone()[0]
        distinct = con.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT po_no, item_no, old_value,"
            " new_value, changed_at FROM po_change_log)").fetchone()[0]
    assert total == distinct, f"變更文件有 {total - distinct} 筆重複"


# ---------------------------------------------------------------------------
# 供應商歷史表現（給生管的決策依據，非預測模型）
# ---------------------------------------------------------------------------
def test_supplier_performance_flags_small_samples():
    """
    樣本太少的統計會誤導人。只有三張單的供應商算出「準交率 33%」，
    那個數字不該拿去做決策，工具寧可說「樣本不足」也不要給假訊號。
    """
    import supplier_stats

    perf = supplier_stats.supplier_performance(min_samples=1000)
    assert not perf["樣本是否足夠"].any(), "門檻拉高後不應有任何供應商通過"

    perf = supplier_stats.supplier_performance(min_samples=1)
    assert perf["樣本是否足夠"].all()
    assert (perf["準交率"].between(0, 1)).all()


def test_reschedule_reliability_validates_repeat_offender_rule():
    """
    這張表是規則 7（累犯）的自我驗證：改期越多次，最終仍延遲的比例應越高。
    若這個關係不成立，規則 7 就該被拿掉。
    """
    import supplier_stats

    t = supplier_stats.reschedule_reliability().set_index("改期情形")
    if {"未改期", "改期 3 次以上"} <= set(t.index):
        assert t.loc["改期 3 次以上", "最終仍延遲比例"] > t.loc["未改期", "最終仍延遲比例"]

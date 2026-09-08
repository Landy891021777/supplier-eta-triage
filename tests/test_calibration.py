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

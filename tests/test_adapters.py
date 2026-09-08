# -*- coding: utf-8 -*-
"""
資料來源 adapter 的一致性測試。

這組測試在守住一件事：**換資料來源不該改變工具的判斷。**

CSV 版本把 `reschedule_count`、`has_qualified_second_source`、
`share_of_period_demand` 直接寫成欄位；
SQLite 版本必須從變更文件、來源清單、請購單用 SQL 推導出來。

兩條路徑算出來的結果必須一致 —— 如果不一致，代表我對 ERP 資料結構的
理解有誤，或推導邏輯寫錯了。這正是接真實 ERP 時最容易出錯、
卻最難察覺的地方：資料照樣跑得出來，只是悄悄地錯。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from adapters import (MATERIAL_CONTRACT, PO_CONTRACT,  # noqa: E402
                      SUPPLIER_CONTRACT, CsvSource, SqliteSource, get_source)

DB = ROOT / "data" / "erp_sim.db"
CSV = ROOT / "data" / "po_master.csv"

pytestmark = pytest.mark.skipif(
    not (DB.exists() and CSV.exists()),
    reason="需先執行 py src/generate_data.py 與 py src/build_erp_db.py")


@pytest.fixture(scope="module")
def sources():
    return CsvSource(), SqliteSource()


# ---------------------------------------------------------------------------
# 資料合約
# ---------------------------------------------------------------------------
def test_both_sources_satisfy_the_contract(sources):
    """
    合約檢查必須在載入時就跑。

    換來源時最常見的失敗不是連不上，而是「連上了但少一欄」，
    然後在下游某條規則裡爆掉，錯誤訊息看起來跟資料來源毫無關聯。
    """
    for src in sources:
        assert src.validate() == [], f"{src.name} 不符合資料合約"


@pytest.mark.parametrize("table,contract", [
    ("purchase_orders", PO_CONTRACT),
    ("materials", MATERIAL_CONTRACT),
    ("suppliers", SUPPLIER_CONTRACT),
])
def test_columns_match_contract(sources, table, contract):
    for src in sources:
        cols = set(getattr(src, table)().columns)
        assert set(contract).issubset(cols), (
            f"{src.name}.{table} 缺少欄位：{set(contract) - cols}")


# ---------------------------------------------------------------------------
# 兩種來源的一致性
# ---------------------------------------------------------------------------
def test_same_purchase_orders(sources):
    csv_src, db_src = sources
    a = csv_src.purchase_orders().set_index("po_no").sort_index()
    b = db_src.purchase_orders().set_index("po_no").sort_index()
    assert list(a.index) == list(b.index), "兩種來源的採購單集合不一致"


@pytest.mark.parametrize("col", ["material_id", "supplier_id", "qty",
                                 "committed_date", "need_date"])
def test_plain_fields_agree(sources, col):
    """這些欄位在兩邊都是直接取用，必須完全相同。"""
    csv_src, db_src = sources
    a = csv_src.purchase_orders().set_index("po_no").sort_index()[col]
    b = db_src.purchase_orders().set_index("po_no").sort_index()[col]
    mismatch = (a.astype(str) != b.astype(str)).sum()
    assert mismatch == 0, f"{col} 有 {mismatch} 筆不一致"


def test_reschedule_count_derived_from_change_log_agrees(sources):
    """
    改期次數：CSV 是現成欄位，SQLite 必須 COUNT 變更文件。
    這是「ERP 裡沒有現成欄位」的代表案例。
    """
    csv_src, db_src = sources
    a = csv_src.purchase_orders().set_index("po_no").sort_index()["reschedule_count"]
    b = db_src.purchase_orders().set_index("po_no").sort_index()["reschedule_count"]
    assert (a.astype(int) != b.astype(int)).sum() == 0


def test_second_source_derived_from_source_list_agrees(sources):
    """
    有無二源：CSV 是布林欄位，SQLite 必須數來源清單裡的合格供應商家數。
    """
    csv_src, db_src = sources
    a = csv_src.materials().set_index("material_id").sort_index()
    b = db_src.materials().set_index("material_id").sort_index()
    diff = (a["has_qualified_second_source"].astype(bool)
            != b["has_qualified_second_source"].astype(bool)).sum()
    assert diff == 0, f"二源判定有 {diff} 筆不一致"


def test_demand_share_agrees_within_rounding(sources):
    """
    佔比：SQLite 由「本單量 ÷ 請購單當期需求量」算出，
    會有整數化的捨入誤差，因此只要求近似相等。
    """
    csv_src, db_src = sources
    a = csv_src.purchase_orders().set_index("po_no").sort_index()["share_of_period_demand"]
    b = db_src.purchase_orders().set_index("po_no").sort_index()["share_of_period_demand"]
    assert (a.astype(float) - b.astype(float)).abs().max() < 0.02


# ---------------------------------------------------------------------------
# 空值：SQL 版本天生沒有 NaN 問題
# ---------------------------------------------------------------------------
def test_sqlite_alt_material_never_nan(sources):
    """
    CSV 的空字串被 pandas 讀成 NaN，曾造成「有替代料 nan」的 bug。
    關聯式模型用「沒有這一列」表示沒有替代料，天然避免這個問題。
    """
    _, db_src = sources
    alt = db_src.materials()["alt_material_id"]
    assert alt.notna().all()
    assert not alt.astype(str).str.lower().isin({"nan", "none"}).any()


# ---------------------------------------------------------------------------
# 註冊表
# ---------------------------------------------------------------------------
def test_unknown_source_fails_loudly():
    """
    找不到指定來源時必須明確拋錯，不可安靜退回預設值。
    使用者以為在讀 ERP、實際卻在讀 CSV，是最糟的失敗方式。
    """
    with pytest.raises(ValueError, match="未知的資料來源"):
        get_source("sap_production")

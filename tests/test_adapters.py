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

import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from adapters import (MATERIAL_CONTRACT, PO_CONTRACT,  # noqa: E402
                      SUPPLIER_CONTRACT, CsvSource, SqliteSource, get_source)
from domain import CATEGORY_SUPPLIER_TYPE  # noqa: E402

DB = ROOT / "data" / "erp_sim.db"
CSV = ROOT / "data" / "po_master.csv"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

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
    """
    分批交貨後同一張單可能有多列，鍵必須是 (po_no, sched_line)，
    否則同一張單兩批會被誤判成「兩邊集合不一致」。
    """
    csv_src, db_src = sources
    a = csv_src.purchase_orders().set_index(["po_no", "sched_line"]).sort_index()
    b = db_src.purchase_orders().set_index(["po_no", "sched_line"]).sort_index()
    assert list(a.index) == list(b.index), "兩種來源的（採購單號、排程行）集合不一致"


@pytest.mark.parametrize("col", ["material_id", "supplier_id", "qty",
                                 "committed_date", "need_date"])
def test_plain_fields_agree(sources, col):
    """
    這些欄位在兩邊都是直接取用，必須完全相同。
    鍵改成 (po_no, sched_line)：分批交貨時 committed_date 是「這一批」的日期，
    只用 po_no 當鍵會把兩批混在一起比較，比出假的不一致。
    """
    csv_src, db_src = sources
    a = csv_src.purchase_orders().set_index(["po_no", "sched_line"]).sort_index()[col]
    b = db_src.purchase_orders().set_index(["po_no", "sched_line"]).sort_index()[col]
    mismatch = (a.astype(str) != b.astype(str)).sum()
    assert mismatch == 0, f"{col} 有 {mismatch} 筆不一致"


def test_reschedule_count_derived_from_change_log_agrees(sources):
    """
    改期次數：CSV 是現成欄位，SQLite 必須 COUNT 變更文件。
    這是「ERP 裡沒有現成欄位」的代表案例。改期次數是項次層級的事實，
    分批交貨的兩批會有相同的值，仍以 (po_no, sched_line) 為鍵比對。
    """
    csv_src, db_src = sources
    a = csv_src.purchase_orders().set_index(["po_no", "sched_line"]).sort_index()["reschedule_count"]
    b = db_src.purchase_orders().set_index(["po_no", "sched_line"]).sort_index()["reschedule_count"]
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
    佔比：SQLite 由「本批量 ÷ 請購單當期需求量」算出（分子是 sched_qty，
    不是項次總量 qty —— 分批交貨時每一批各自佔當期需求的一部分），
    會有整數化的捨入誤差，因此只要求近似相等。
    """
    csv_src, db_src = sources
    a = csv_src.purchase_orders().set_index(["po_no", "sched_line"]).sort_index()["share_of_period_demand"]
    b = db_src.purchase_orders().set_index(["po_no", "sched_line"]).sort_index()["share_of_period_demand"]
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


# ---------------------------------------------------------------------------
# 新世界（晶圓廠）：收貨處理天數與計量單位
#
# 不能用真實 data/：那裡目前還是舊世界（Fabless），要到 Task 10 才重新產生。
# 這裡在 tmp_path 內用新版產生器重建一份完整的新世界 CSV／模擬 ERP，
# 只驗證這個 Task 動到的東西：料號主檔的 base_uom／gr_processing_days，
# 以及來源清單的供應商類型是否對得上料別。
# ---------------------------------------------------------------------------
@pytest.fixture
def new_world_db(tmp_path, monkeypatch):
    import build_erp_db
    import generate_data

    monkeypatch.setattr(generate_data, "DATA", tmp_path)
    monkeypatch.setattr(generate_data, "INBOX", tmp_path / "inbox")
    monkeypatch.setattr(build_erp_db, "DATA", tmp_path)
    monkeypatch.setattr(build_erp_db, "DB_PATH", tmp_path / "erp_sim.db")

    generate_data.main()
    build_erp_db.build(verbose=False)
    return tmp_path / "erp_sim.db"


def test_material_master_carries_base_uom_and_gr_processing_days(new_world_db):
    """
    收貨處理天數（≈MARC-WEBAZ）與計量單位要能從模擬 ERP 讀回來，
    且要等於 config.yaml 的料別預設 —— 分級公式之後要靠這兩欄算可投產日。
    """
    src = SqliteSource(new_world_db)
    mats = src.materials().set_index("material_id")
    days = CFG["receiving"]["gr_processing_days"]
    assert {"base_uom", "gr_processing_days"} <= set(mats.columns)
    for mid, row in mats.iterrows():
        assert row["gr_processing_days"] == days[row["category"]], mid
        assert row["base_uom"], mid


def test_cmp_slurry_never_has_a_second_source(new_world_db):
    """
    研磨液（CMP_SLURRY）全公司只有 SUP-L01 一家，不該有已認證二源；
    否則畫面會暗示「還有牌可打」，但實際上沒有別家可轉單。
    """
    src = SqliteSource(new_world_db)
    mats = src.materials()
    slurry = mats[mats["category"] == "CMP_SLURRY"]
    assert not slurry.empty
    assert not slurry["has_qualified_second_source"].any()


def test_source_list_vendor_type_matches_material_category(new_world_db):
    """
    來源清單裡每個料號的合格供應商類型，都要等於這個料本身的供應商類型；
    否則畫面上會出現「光阻缺料，聯絡的卻是氣體供應商」這種一眼穿幫的破綻。

    也要求每個料號至少有一筆來源：舊版用 type_of_cat（只認得舊世界的
    WAFER/MASK/SUBSTRATE/ASSEMBLY）對新料別一律查不到，cands 變空列表，
    導致新世界裡四成料號的來源清單整段空白 —— 不會報錯，只是悄悄地錯。
    """
    with sqlite3.connect(new_world_db) as con:
        n_materials = con.execute("SELECT COUNT(*) FROM material_master").fetchone()[0]
        rows = con.execute("""
            SELECT sl.material_id, m.category, v.vendor_type
            FROM source_list sl
            JOIN material_master m ON m.material_id = sl.material_id
            JOIN vendor_master v ON v.vendor_id = sl.vendor_id
        """).fetchall()
    assert rows
    assert len({r[0] for r in rows}) == n_materials, "有料號在來源清單裡完全沒有供應商"
    for material_id, category, vendor_type in rows:
        assert vendor_type == CATEGORY_SUPPLIER_TYPE[category], (
            material_id, category, vendor_type)


# ---------------------------------------------------------------------------
# 分批交貨（Plan 3 Task 1）：交貨排程行改成每行一列
#
# pipeline／畫面／匯出還沒對位到排程行（那是 Task 3 的範圍），這裡只驗證
# 資料層：兩個來源是不是都老實地把每一批當成獨立的一列回傳。
# ---------------------------------------------------------------------------
@pytest.fixture
def new_world_sources(new_world_db):
    return CsvSource(new_world_db.parent), SqliteSource(new_world_db)


def test_purchase_orders_has_one_row_per_schedule_line(new_world_sources):
    """
    SqliteSource 過去用 latest_sched CTE 把同一項次的排程行壓成一列
    （取最晚的一筆），分批交貨時先到的那批因此完全消失。改成每行一列後，
    同一張單有兩筆排程行時 purchase_orders() 就該回傳兩列，
    po_no 相同、sched_line 不同、且不能重複。
    """
    _, db_src = new_world_sources
    po = db_src.purchase_orders()
    assert {"sched_line", "sched_qty"} <= set(po.columns)
    dup = po[po.duplicated(["po_no", "sched_line"], keep=False)]
    assert dup.empty, f"同一張單同一排程行出現多列：{dup['po_no'].tolist()}"

    split_pos = po.groupby("po_no").filter(lambda g: len(g) > 1)
    assert not split_pos.empty, "新世界資料裡沒有任何分批交貨的單，測試前提不成立"
    for po_no, g in split_pos.groupby("po_no"):
        assert sorted(g["sched_line"]) == list(range(1, len(g) + 1)), po_no
        assert g["sched_qty"].sum() == g["qty"].iloc[0], (
            po_no, "各批數量加總應等於項次總量")


def test_single_line_pos_behave_exactly_as_before(new_world_sources):
    """
    回歸：沒有分批的單，sched_line 固定是 1、sched_qty 等於 qty ——
    行為要跟改版前「每張單一列」完全一樣，不能因為支援分批就連帶動到多數單。
    """
    _, db_src = new_world_sources
    po = db_src.purchase_orders()
    single = po.groupby("po_no").filter(lambda g: len(g) == 1)
    assert not single.empty
    assert (single["sched_line"] == 1).all()
    assert (single["sched_qty"] == single["qty"]).all()


def test_csv_source_also_has_sched_line_and_sched_qty(new_world_sources):
    """CSV 來源（po_master.csv）也要有 sched_line／sched_qty，單一排程行時 sched_line=1。"""
    csv_src, _ = new_world_sources
    po = csv_src.purchase_orders()
    assert {"sched_line", "sched_qty"} <= set(po.columns)
    single = po.groupby("po_no").filter(lambda g: len(g) == 1)
    assert (single["sched_line"] == 1).all()
    assert (single["sched_qty"] == single["qty"]).all()


def test_csv_and_sqlite_agree_on_po_and_schedule_line_set(new_world_sources):
    """
    兩個資料來源對「哪些單有幾批」的看法必須一致，否則換資料來源時
    行動清單的筆數會對不上，而且沒有明顯的錯誤訊息。
    """
    csv_src, db_src = new_world_sources
    a = set(map(tuple, csv_src.purchase_orders()[["po_no", "sched_line"]].values))
    b = set(map(tuple, db_src.purchase_orders()[["po_no", "sched_line"]].values))
    assert a == b


def test_hc010_po_is_a_single_unsplit_line_in_both_sources(new_world_sources):
    """
    手寫案例 HC-010 依賴 PO-2026-04188 在 ERP 裡是**單一**排程行
    （20 片 @ 2026-09-30，尚未拆行）——HC-010 的信是供應商「提議」把這
    一行拆成 8 片照原日期、12 片延到 2026-11-15，不是複述 ERP 已經拆好
    的排程。如果 ERP 預先就拆成兩行，兩批新日期會剛好等於各自排程行的
    committed_date，觸發對位後兩批都判成 no_change，延遲的 12 片就
    憑空消失了（見決策 19 的修正）。這是固定資料，兩種來源都要一致。
    """
    csv_src, db_src = new_world_sources
    for src in (csv_src, db_src):
        rows = (src.purchase_orders()
                .query("po_no == 'PO-2026-04188'")
                .sort_values("sched_line"))
        assert list(rows["sched_line"]) == [1], src.name
        assert list(rows["sched_qty"]) == [20], src.name
        assert list(rows["committed_date"].astype(str).str[:10]) == [
            "2026-09-30"], src.name
        assert (rows["qty"] == 20).all(), src.name

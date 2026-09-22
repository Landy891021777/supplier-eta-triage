# -*- coding: utf-8 -*-
"""
模擬 ERP（SQLite）資料來源。

這個 adapter 的重點不是「會用 SQLite」，而是：
**它必須用真正的 JOIN 去推導那些在 ERP 裡不存在現成欄位的資料。**

下面三段 SQL 對應三個整合時真正會遇到的問題：

  1. 承諾日不在採購單頭，在交貨排程行 (po_schedule)。
     一個項次可能有多筆排程行（分批交貨），要決定取哪一筆。
     本版取最晚的一筆，並在 README 標為已知簡化。

  2. 改期次數不是一個欄位，是變更文件裡「承諾日被改」的筆數。
     必須 COUNT 出來。

  3. 有沒有二源不是布林欄位，是「來源清單裡除了本供應商之外，
     還有沒有其他合格供應商」。必須 EXISTS 判斷。

CSV 版本把這三件事都預先算好寫在欄位裡，等於跳過了整合的難處。
把它們寫成 SQL，才算真的面對過這個問題。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from .base import DataSource

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = ROOT / "data" / "erp_sim.db"


# ---------------------------------------------------------------------------
# 採購單視圖：把散在六張表的資訊組回工具需要的樣子
# ---------------------------------------------------------------------------
PO_SQL = """
WITH latest_sched AS (
    -- 承諾日：取該項次最晚的一筆排程行。
    -- 分批交貨時這是簡化處理（見 README 已知限制）：
    -- 正確做法是把每一筆排程行當成獨立的追蹤單位。
    SELECT po_no, item_no,
           MAX(committed_date) AS committed_date,
           SUM(qty)            AS sched_qty
    FROM po_schedule
    GROUP BY po_no, item_no
),
reschedules AS (
    -- 改期次數：變更文件中「承諾日」被修改的筆數。
    -- 真實 ERP 沒有現成欄位，這就是它真正的樣子。
    SELECT po_no, item_no, COUNT(*) AS reschedule_count
    FROM po_change_log
    WHERE field_name = 'committed_date'
    GROUP BY po_no, item_no
)
SELECT
    h.po_no                                   AS po_no,
    i.material_id                             AS material_id,
    h.vendor_id                               AS supplier_id,
    i.qty                                     AS qty,
    s.committed_date                          AS committed_date,
    r.need_date                               AS need_date,
    COALESCE(r.downstream_scheduled, 0)       AS downstream_scheduled,
    COALESCE(rs.reschedule_count, 0)          AS reschedule_count,
    -- 本單數量佔該料號當期需求的比例。
    -- 分母來自請購單，不是採購單 —— 需求是需求，採購是採購。
    CASE WHEN COALESCE(r.period_demand_qty, 0) > 0
         THEN ROUND(CAST(i.qty AS REAL) / r.period_demand_qty, 4)
         ELSE 1.0 END                         AS share_of_period_demand,
    h.created_date                            AS po_created_date
FROM po_header h
JOIN po_item      i  ON i.po_no = h.po_no
JOIN latest_sched s  ON s.po_no = i.po_no AND s.item_no = i.item_no
LEFT JOIN purchase_req r  ON r.pr_no  = i.pr_no
LEFT JOIN reschedules  rs ON rs.po_no = i.po_no AND rs.item_no = i.item_no
LEFT JOIN goods_receipt g ON g.po_no  = i.po_no AND g.item_no = i.item_no
-- 只取「在途」採購單：已收貨的單不該出現在今日行動清單裡。
-- 這個條件在 CSV 版本不存在，因為 CSV 裡壓根沒有歷史單 ——
-- 這正是扁平檔案掩蓋掉的另一個現實：真實 ERP 裡未結與已結的單混在一起，
-- 「哪些還要追」本身就是一個要靠 JOIN 判斷的問題。
WHERE g.gr_no IS NULL
ORDER BY h.po_no
"""

# ---------------------------------------------------------------------------
# 料號視圖
# ---------------------------------------------------------------------------
MATERIAL_SQL = """
SELECT
    m.material_id,
    m.category,
    m.std_lead_time_days,
    CASE WHEN m.is_bottleneck = 1 THEN 1 ELSE 0 END AS is_bottleneck,
    -- 有沒有已認證二源：來源清單裡合格供應商是否 2 家以上。
    -- 這不是一個布林欄位，是一個要算出來的事實。
    CASE WHEN (
        SELECT COUNT(*) FROM source_list sl
        WHERE sl.material_id = m.material_id AND sl.is_qualified = 1
    ) >= 2 THEN 1 ELSE 0 END                        AS has_qualified_second_source,
    -- 替代料：沒有就是沒有這一列，而不是空字串。
    -- CSV 版本的空字串被 pandas 讀成 NaN，曾造成「有替代料 nan」的 bug；
    -- 關聯式模型天然避免了這個問題。
    COALESCE((
        SELECT ma.alt_material_id FROM material_alternate ma
        WHERE ma.material_id = m.material_id LIMIT 1
    ), '')                                          AS alt_material_id,
    m.criticality,
    m.base_uom,
    m.gr_processing_days
FROM material_master m
ORDER BY m.material_id
"""

# 舊版資料庫相容：Task 4（收貨處理天數）之前建立的模擬 ERP，
# material_master 沒有 base_uom／gr_processing_days 這兩欄。
# 真要重建要到 Task 10 才會用新版 build_erp_db.py，在那之前
# 用 NULL／0 補齊，讓舊資料庫仍讀得動（0 天代表「主檔沒維護，視為當天可用」）。
MATERIAL_SQL_LEGACY = MATERIAL_SQL.replace(
    "    m.base_uom,\n    m.gr_processing_days\n",
    "    NULL AS base_uom,\n    0 AS gr_processing_days\n",
)

SUPPLIER_SQL = """
SELECT vendor_id   AS supplier_id,
       vendor_name AS supplier_name,
       vendor_type AS supplier_type,
       otd_rate    AS historical_otd_rate
FROM vendor_master
ORDER BY vendor_id
"""


class SqliteSource(DataSource):
    """從模擬 ERP 資料庫讀取，欄位以 SQL 推導而非直接取用。"""

    name = "sqlite"

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or DEFAULT_DB)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"找不到模擬 ERP 資料庫 {self.db_path}，"
                "請先執行： py src/build_erp_db.py")
        self._cache: dict[str, pd.DataFrame] = {}

    def _q(self, key: str, sql: str) -> pd.DataFrame:
        if key not in self._cache:
            with sqlite3.connect(self.db_path) as con:
                self._cache[key] = pd.read_sql_query(sql, con)
        return self._cache[key].copy()

    def _material_sql(self) -> str:
        """舊版資料庫沒有 base_uom／gr_processing_days 欄位時，改用相容版 SQL。"""
        with sqlite3.connect(self.db_path) as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info(material_master)")}
        if {"base_uom", "gr_processing_days"} <= cols:
            return MATERIAL_SQL
        return MATERIAL_SQL_LEGACY

    def purchase_orders(self) -> pd.DataFrame:
        df = self._q("po", PO_SQL)
        df["downstream_scheduled"] = df["downstream_scheduled"].astype(bool)
        return df

    def materials(self) -> pd.DataFrame:
        df = self._q("mat", self._material_sql())
        df["is_bottleneck"] = df["is_bottleneck"].astype(bool)
        df["has_qualified_second_source"] = (
            df["has_qualified_second_source"].astype(bool))
        # 缺值代表主檔沒維護，視為當天可用（0 天），而不是讓下游算式碰到 NaN。
        df["gr_processing_days"] = df["gr_processing_days"].fillna(0).astype(int)
        return df

    def suppliers(self) -> pd.DataFrame:
        return self._q("sup", SUPPLIER_SQL)

    def describe(self) -> str:
        return f"模擬 ERP 資料庫（SQLite，欄位以 SQL JOIN 推導）：{self.db_path.name}"

    # -----------------------------------------------------------------
    def document_trail(self, po_no: str) -> dict[str, pd.DataFrame]:
        """
        單據軌跡：一張採購單在 ERP 裡實際散落成哪些單據。

        這是給使用者看的「往下鑽」畫面 —— 也是最能說明
        「為什麼整合 ERP 不是撈一張表就好」的一頁。
        一張採購單的完整故事橫跨六張表：
        請購 → 採購單頭 → 項次 → 交貨排程 → 變更紀錄 → 收貨。
        """
        queries = {
            "① 請購單 (≈EBAN)": """
                SELECT r.pr_no AS 請購單號, r.material_id AS 料號,
                       r.req_qty AS 請購量, r.need_date AS 下游需求日,
                       r.period_demand_qty AS 當期總需求,
                       CASE WHEN r.downstream_scheduled=1 THEN '是' ELSE '否' END AS 下游已排定
                FROM po_item i JOIN purchase_req r ON r.pr_no = i.pr_no
                WHERE i.po_no = ?""",
            "② 採購單頭 (≈EKKO)": """
                SELECT h.po_no AS 採購單號, h.vendor_id AS 供應商,
                       v.vendor_name AS 供應商名稱, h.created_date AS 建立日
                FROM po_header h LEFT JOIN vendor_master v ON v.vendor_id = h.vendor_id
                WHERE h.po_no = ?""",
            "③ 採購單項次 (≈EKPO)": """
                SELECT po_no AS 採購單號, item_no AS 項次,
                       material_id AS 料號, qty AS 數量, pr_no AS 來源請購單
                FROM po_item WHERE po_no = ?""",
            "④ 交貨排程行 (≈EKET)　承諾日在這裡": """
                SELECT po_no AS 採購單號, item_no AS 項次, sched_line AS 排程行,
                       committed_date AS 承諾交期, qty AS 數量
                FROM po_schedule WHERE po_no = ? ORDER BY sched_line""",
            "⑤ 變更文件 (≈CDHDR/CDPOS)　改期次數要 COUNT 這張": """
                SELECT changed_at AS 變更日, field_name AS 欄位,
                       old_value AS 原值, new_value AS 新值, changed_by AS 變更者
                FROM po_change_log WHERE po_no = ? ORDER BY changed_at""",
            "⑥ 收貨紀錄 (≈MKPF/MSEG)　實際到料日": """
                SELECT gr_no AS 收貨單號, qty AS 收貨量, receipt_date AS 實際到料日
                FROM goods_receipt WHERE po_no = ?""",
        }
        out: dict[str, pd.DataFrame] = {}
        with sqlite3.connect(self.db_path) as con:
            for label, sql in queries.items():
                out[label] = pd.read_sql_query(sql, con, params=(po_no,))
        return out

    def open_po_numbers(self, limit: int = 400) -> list[str]:
        """尚未收貨的採購單（在途）。"""
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute("""
                SELECT i.po_no FROM po_item i
                LEFT JOIN goods_receipt g
                       ON g.po_no = i.po_no AND g.item_no = i.item_no
                WHERE g.gr_no IS NULL
                ORDER BY i.po_no LIMIT ?""", (limit,)).fetchall()
        return [r[0] for r in rows]

    def closed_po_numbers(self, limit: int = 400) -> list[str]:
        """已收貨結案的採購單（有實際到料日，供供應商歷史統計與回測使用）。"""
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute(
                "SELECT DISTINCT po_no FROM goods_receipt ORDER BY po_no LIMIT ?",
                (limit,)).fetchall()
        return [r[0] for r in rows]

    def table_counts(self) -> dict[str, int]:
        """給 UI 顯示各表筆數，讓使用者看得到資料實際長什麼樣。"""
        tables = ("vendor_master", "material_master", "material_alternate",
                  "source_list", "purchase_req", "po_header", "po_item",
                  "po_schedule", "po_change_log", "goods_receipt")
        with sqlite3.connect(self.db_path) as con:
            return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in tables}

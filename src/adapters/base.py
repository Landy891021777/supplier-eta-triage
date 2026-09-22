# -*- coding: utf-8 -*-
"""
資料來源介面（資料合約）。

===========================  為什麼需要這一層  ===========================
工具的判斷邏輯不該知道資料是從哪裡來的。

第一版讀 CSV，示範時很方便；但真要導入，資料會來自 ERP —— 可能是
IT 開的唯讀 view、每日拋轉的中繼表、或 OData/RFC 介面。
如果規則與評分直接寫死 `pd.read_csv`，換來源就要動到業務邏輯，
而業務邏輯一動就要重新驗證。這是內部工具最常見的技術債。

所以這裡定義「資料合約」：不管來源是什麼，都必須提供這幾張表、
這些欄位、這些型別。實作細節（要 JOIN 幾張表、欄位叫什麼）
由各 adapter 自己處理。

目前提供兩個實作：
    CsvSource     讀 data/*.csv，最簡單，適合快速展示
    SqliteSource  讀模擬 ERP 資料庫，用真正的 SQL JOIN 推導欄位

真要接公司 ERP 時，只需要再寫一個 adapter，規則、評分、介面
一行都不用改。欄位對應表見 docs/ERP欄位對應.md。
=========================================================================
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

# ---------------------------------------------------------------------------
# 資料合約：每個 adapter 都必須提供這些欄位
# ---------------------------------------------------------------------------
# 註記「推導」的欄位，在真實 ERP 裡不是現成的一欄，必須自己算出來。
# 這正是 CSV 版本偷偷跳過、而 SQL 版本必須面對的部分。
PO_CONTRACT = {
    "po_no": "採購單號",
    "material_id": "料號",
    "supplier_id": "供應商代號",
    "qty": "採購數量",
    "committed_date": "供應商承諾交期（推導：交貨排程行中最晚的一筆）",
    "need_date": "下游需求日（推導：來自請購單）",
    "downstream_scheduled": "下游是否已排定產能或已對客戶承諾",
    "reschedule_count": "已改期次數（推導：變更文件中承諾日被改的次數）",
    "share_of_period_demand": "本單數量佔該料號當期需求比例（推導：本單量 ÷ 當期需求量）",
    "po_created_date": "採購單建立日",
}

MATERIAL_CONTRACT = {
    "material_id": "料號",
    "category": "料號類別",
    "std_lead_time_days": "標準前置期（天）",
    "is_bottleneck": "是否為瓶頸料",
    "has_qualified_second_source": "是否有已認證二源（推導：來源清單中合格供應商是否 ≥ 2 家）",
    "alt_material_id": "替代料號（推導：替代料關係表）",
    "criticality": "關鍵性等級",
    "base_uom": "基本計量單位",
    "gr_processing_days": "收貨處理天數（到廠後幾天才能投產；舊版資料庫沒有此欄位時補 0）",
}

SUPPLIER_CONTRACT = {
    "supplier_id": "供應商代號",
    "supplier_name": "供應商名稱",
    "supplier_type": "供應商類型",
    "historical_otd_rate": "歷史準交率（真實環境應由收貨紀錄回算）",
}


class DataSource(ABC):
    """所有資料來源的共同介面。"""

    name: str = "base"

    @abstractmethod
    def purchase_orders(self) -> pd.DataFrame:
        """未結採購單，欄位須符合 PO_CONTRACT。"""

    @abstractmethod
    def materials(self) -> pd.DataFrame:
        """料號主檔，欄位須符合 MATERIAL_CONTRACT。"""

    @abstractmethod
    def suppliers(self) -> pd.DataFrame:
        """供應商主檔，欄位須符合 SUPPLIER_CONTRACT。"""

    def describe(self) -> str:
        """給 UI 顯示的來源說明 —— 使用者有權知道資料是從哪裡來的。"""
        return self.name

    # -----------------------------------------------------------------
    def validate(self) -> list[str]:
        """
        檢查這個 adapter 是否真的滿足資料合約。

        存在的理由很現實：換資料來源時最常見的失敗不是連不上，
        而是「連上了，但少了一欄」或「型別不對」，然後在下游
        某條規則裡爆掉，錯誤訊息看起來跟資料來源完全無關。
        寧可在載入時就大聲失敗，也不要在評分到一半時安靜出錯。
        """
        problems: list[str] = []
        for df, contract, label in (
            (self.purchase_orders(), PO_CONTRACT, "採購單"),
            (self.materials(), MATERIAL_CONTRACT, "料號主檔"),
            (self.suppliers(), SUPPLIER_CONTRACT, "供應商主檔"),
        ):
            missing = [c for c in contract if c not in df.columns]
            if missing:
                problems.append(f"{label} 缺少欄位：{', '.join(missing)}")
            if df.empty:
                problems.append(f"{label} 沒有任何資料")
        return problems

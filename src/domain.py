# -*- coding: utf-8 -*-
"""
領域常數與型別定義。

本檔集中定義「這個工具眼中的世界」——料號類別、供應商類型、承諾強度、
變更原因。之所以獨立成檔，是因為這些是跟採購與物料企劃同仁溝通時的共同語言，
未來要增修時應該只動這一個地方。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class MaterialCategory(str, Enum):
    """
    晶圓廠（前段製造）的生產用料類別。

    範圍是物料企劃追的「生產用料」，不含備品（MRO）：備品屬設備工程的補料邏輯，
    追料方式和生產用料不同。
    """
    SILICON_WAFER = "SILICON_WAFER"    # 矽晶圓原片（拋光片、磊晶片）
    PHOTORESIST = "PHOTORESIST"        # 光阻
    SPECIALTY_GAS = "SPECIALTY_GAS"    # 特殊氣體
    WET_CHEMICAL = "WET_CHEMICAL"      # 濕式化學品
    TARGET = "TARGET"                  # 濺鍍靶材
    MASK = "MASK"                      # 光罩
    CMP_SLURRY = "CMP_SLURRY"          # 研磨液


class SupplierType(str, Enum):
    WAFER_MAKER = "WAFER_MAKER"
    RESIST_MAKER = "RESIST_MAKER"
    GAS_SUPPLIER = "GAS_SUPPLIER"
    CHEMICAL_SUPPLIER = "CHEMICAL_SUPPLIER"
    TARGET_MAKER = "TARGET_MAKER"
    MASK_SHOP = "MASK_SHOP"
    SLURRY_MAKER = "SLURRY_MAKER"


# 領域假設：一個料別只向一種供應商類型採購 —— 光阻不會去跟氣體廠買。
# 產生資料、建模擬 ERP 的來源清單都用這張表，避免兩邊各寫一份而對不上。
CATEGORY_SUPPLIER_TYPE = {
    MaterialCategory.SILICON_WAFER.value: SupplierType.WAFER_MAKER.value,
    MaterialCategory.PHOTORESIST.value: SupplierType.RESIST_MAKER.value,
    MaterialCategory.SPECIALTY_GAS.value: SupplierType.GAS_SUPPLIER.value,
    MaterialCategory.WET_CHEMICAL.value: SupplierType.CHEMICAL_SUPPLIER.value,
    MaterialCategory.TARGET.value: SupplierType.TARGET_MAKER.value,
    MaterialCategory.MASK.value: SupplierType.MASK_SHOP.value,
    MaterialCategory.CMP_SLURRY.value: SupplierType.SLURRY_MAKER.value,
}

# 畫面與知識卡用中文顯示；企劃問「光阻廠準不準」時，檢索才對得到字。
CATEGORY_LABEL_ZH = {
    "SILICON_WAFER": "矽晶圓", "PHOTORESIST": "光阻", "SPECIALTY_GAS": "特殊氣體",
    "WET_CHEMICAL": "濕式化學品", "TARGET": "靶材", "MASK": "光罩",
    "CMP_SLURRY": "研磨液",
}
SUPPLIER_TYPE_LABEL_ZH = {
    "WAFER_MAKER": "矽晶圓廠", "RESIST_MAKER": "光阻廠", "GAS_SUPPLIER": "特殊氣體廠",
    "CHEMICAL_SUPPLIER": "化學品廠", "TARGET_MAKER": "靶材廠", "MASK_SHOP": "光罩廠",
    "SLURRY_MAKER": "研磨液廠",
}

# 領域假設：各料別的標準前置期、計量單位與常見下單量。
#   前置期是「下單到到廠」的合約天數；12 吋矽晶圓與靶材最長，化學品最短。
#   跟 CATEGORY_SUPPLIER_TYPE 放在同一個檔案的理由相同：合成資料產生器
#   （generate_data.py／generate_history.py）都要用同一份數量選項，
#   不能各寫一份——那正是歷史單「數量跟料別對不起來」這個 bug 的成因。
CATEGORY_SPEC = {
    "SILICON_WAFER": {"lt": (60, 120), "uom": "PCS", "qty": [500, 1000, 1500, 2000, 3000, 5000]},
    "PHOTORESIST":   {"lt": (30, 90),  "uom": "GAL", "qty": [20, 40, 80, 120, 200]},
    "SPECIALTY_GAS": {"lt": (20, 60),  "uom": "CYL", "qty": [10, 20, 40, 60, 100]},
    "WET_CHEMICAL":  {"lt": (10, 30),  "uom": "DRM", "qty": [20, 40, 80, 160]},
    "TARGET":        {"lt": (45, 100), "uom": "PCS", "qty": [2, 4, 8, 12, 20, 30]},
    "MASK":          {"lt": (14, 35),  "uom": "PCS", "qty": [1]},
    "CMP_SLURRY":    {"lt": (20, 50),  "uom": "GAL", "qty": [50, 100, 200, 400]},
}
CATEGORY_WEIGHTS = {"SILICON_WAFER": .25, "PHOTORESIST": .15, "SPECIALTY_GAS": .15,
                    "WET_CHEMICAL": .15, "TARGET": .10, "MASK": .10, "CMP_SLURRY": .10}


class CommitmentStrength(str, Enum):
    """
    承諾強度 —— 本專案最重要的一個欄位。

    供應商回信說「大概月底吧，我再跟你確認」，如果工具把它抽成
    2026-10-31 寫進表格，生管看到一個確切日期就會以為事情定了。
    這是把不確定性洗掉，比不解析還危險。

    因此解析層必須額外判斷「這句話到底算不算承諾」，
    非 CONFIRMED 的一律不覆寫系統承諾日，並強制人工再追一次。
    """
    CONFIRMED = "confirmed"      # 明確承諾：「Revised ETA 10/30 confirmed」
    ESTIMATED = "estimated"      # 暫估：「預計月底」「大概晚兩週」
    INTENT_ONLY = "intent_only"  # 僅表達意向：「我們盡量」「再跟你確認」
    NONE = "none"                # 信中未提及新日期


class ChangeType(str, Enum):
    DELAY = "delay"          # 延遲
    PULL_IN = "pull_in"      # 提前（也需要處理：可能要提早備料、提早付款）
    NO_CHANGE = "no_change"  # 確認照原計畫（重要：不能誤判成延遲）
    UNKNOWN = "unknown"


# 供應商在信中會提到的變更原因。分類的目的不是統計好看，
# 而是因為不同原因的「可信度」與「後續行動」完全不同：
#   - 產能排擠：通常還有喬的空間，值得打電話
#   - 良率/製程異常：沒得喬，要立刻找替代來源
#   - 上游缺料：要往上追第二層供應商
REASON_CODES = {
    "capacity": "產能排擠 / loading 滿載",
    "yield": "製程或品質異常",
    "upstream_shortage": "上游原材料短缺",
    "logistics": "運輸 / 通關延誤",
    "customer_priority": "其他客戶插單優先",
    "internal_reschedule": "供應商內部重排",
    "not_stated": "未說明原因",
}


@dataclass
class ExtractedRecord:
    """單一封信中，針對某一張 PO 解析出來的一筆結果。"""
    po_no: str | None
    material_id: str | None
    new_eta: str | None                  # ISO 格式 YYYY-MM-DD；無法判定則 None
    raw_date_text: str | None            # 原文中的日期字樣，供人工覆核
    commitment_strength: str = CommitmentStrength.NONE.value
    change_type: str = ChangeType.UNKNOWN.value
    reason_code: str = "not_stated"
    confidence: float = 0.0              # 0~1，解析層對自己的信心
    extracted_by: str = "rule"           # rule | llm | none
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EmailMessage:
    email_id: str
    sender: str
    supplier_id: str
    received_at: str
    subject: str
    body: str
    style: str = ""                      # 僅供評估用，實務上不會有這個欄位
    tags: list[str] = field(default_factory=list)

# -*- coding: utf-8 -*-
"""
領域常數與型別定義。

本檔集中定義「這個工具眼中的世界」——料號類別、供應商類型、承諾強度、
變更原因。之所以獨立成檔，是因為這些是跟採購/生管同仁溝通時的共同語言，
未來要增修時應該只動這一個地方。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class MaterialCategory(str, Enum):
    """
    Fabless 情境下的委外料件類別。

    本專案範圍鎖定 WAFER 段（晶圓代工），因為那是我實際待過、
    特徵判斷最有把握的環節。SUBSTRATE / ASSEMBLY 已預留欄位與介面，
    但未實作對應的專屬規則——理由詳見 docs/設計決策.md。
    """
    WAFER = "WAFER"            # 晶圓代工投片
    MASK = "MASK"              # 光罩
    SUBSTRATE = "SUBSTRATE"    # 載板（預留）
    ASSEMBLY = "ASSEMBLY"      # 封裝（預留）


class SupplierType(str, Enum):
    FOUNDRY = "FOUNDRY"
    MASK_SHOP = "MASK_SHOP"
    SUBSTRATE = "SUBSTRATE"
    OSAT = "OSAT"


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
    "yield": "良率或製程異常",
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

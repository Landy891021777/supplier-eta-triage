# -*- coding: utf-8 -*-
"""
交期風險分級：預估缺料天數 ＋ P1/P2/P3 ＋ 建議動作。

===========================  為什麼沒有權重  ===========================
舊版用十條規則各給 0~1 分、再依權重加成 0~100 分。那組權重是人手填的，
拿去「校準」時用的又是自己產生的歷史資料，等於用答案驗答案
（見 docs/設計決策.md 決策 16）。

這一版只問物料企劃真正在意的一件事：這張單到料時，需求日已經過了幾天？

    預估缺料天數 = 保守到料日 − 下游需求日
    保守到料日   = 供應商說的日期 + 這家供應商過去「說定後仍延遲」的天數

那是兩個日期相減，可以在會議上逐項驗算，不是分數。
分級只用「有沒有缺料」與三個事實旗標，沒有任何可調權重。
=====================================================================

領域假設：`need_date` 視為 MRP 的淨需求日（已扣庫存與在途），
因此不另外建庫存表。若接的是未扣庫存的需求日，缺料天數會被高估。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from domain import ChangeType

# 排序用：分級由前到後，同級內再依預估缺料天數由大到小。
PRIORITY_RANK = {"P1": 0, "P2": 1, "P3": 2, "待查": 3, "—": 4}


def _d(v) -> date | None:
    """
    寬鬆的日期正規化。

    來源可能是 ISO 字串、datetime.date、或 pandas Timestamp。
    Timestamp 與 datetime 都是 date 的子類，但直接相減型別會出錯，
    所以必須先降階成純 date。
    """
    if v is None or v != v:  # None 或 NaN
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _clean_str(v) -> str:
    """
    把「空值」正規化成空字串。

    這個函式是因為一個真實的 bug 而生的：料號主檔的替代料欄位在 CSV 裡
    是空字串，pandas 讀進來變成 float NaN，而 str(NaN) 是字串 "nan"，
    它是非空字串，於是通過 `if alt:`，工具就對企劃說「有替代料 nan」。
    空值處理在資料型工具裡不是細節，是正確性問題。
    """
    if v is None or v != v:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none", "nat", "null", "-"} else s


def percentile_for(strength: str | None, cfg: dict) -> float:
    """
    承諾強度越弱，取越保守的歷史百分位。

    領域假設：暫估與僅意向的日期比確認的日期更不可靠，
    「沒有給日期」視為最不可靠，與僅意向同級。
    百分位數字本身是假設，回測只能檢驗它們的涵蓋率，不能證明它們「對」。
    """
    key = {"confirmed": "percentile_confirmed",
           "estimated": "percentile_estimated"}.get(strength or "",
                                                    "percentile_intent_only")
    return float(cfg[key])


def _tier(gap: int, po: dict, material: dict, tight_days: int) -> str:
    critical = bool(po.get("downstream_scheduled")) or bool(material.get("is_bottleneck"))
    single_source = not bool(material.get("has_qualified_second_source"))
    if gap > 0:
        return "P1" if critical else "P2"
    if single_source and gap >= -tight_days:
        return "P2"
    return "P3"


def suggest_actions(priority: str, gap: int, record: dict, po: dict,
                    material: dict) -> list[str]:
    """
    給物料企劃「自己能做」的下一步。

    詢價與下單是採購的工作、驗證替代料是品保的工作，所以動作寫成
    「與採購確認」「請品保確認」，不寫成企劃自己去做。
    成本與可行性資料庫裡沒有，因此不寫。
    """
    if priority not in ("P1", "P2"):
        return []
    acts: list[str] = []
    if record.get("commitment_strength") != "confirmed":
        acts.append("先向供應商追一個可承諾的確切日期（目前日期尚未確認）")
    if gap > 0:
        acts.append("可評估的手段：催貨、拉貨、分批交、空運"
                    "（成本與可行性需與採購、供應商確認）")
    if material.get("has_qualified_second_source"):
        acts.append("與採購確認能否向已認證二源詢價備案")
    elif gap > 0:
        acts.append("單一來源，無已認證二源可轉單，只能追供應商")
    alt = _clean_str(material.get("alt_material_id"))
    if alt:
        acts.append(f"替代料 {alt}：請品保確認是否已通過驗證，或能否走特採；"
                    "未確認前不可視為可用")
    if po.get("downstream_scheduled"):
        acts.append("通知生管：這張單可能缺料，需確認下游排程")
    return acts


def evaluate(record: dict, po: dict, material: dict, triage_cfg: dict,
             estimate: dict | None) -> dict:
    """
    對一筆已對位的變更算預估缺料天數、分級與建議動作。

    estimate 是 supplier_stats.estimate_delay 的結果（由呼叫端依承諾強度
    選好百分位後傳入），本函式不讀資料庫，方便單獨測試。
    """
    change = record.get("change_type")
    out = {"gap_days": None, "conservative_eta": None, "delay_days_est": 0,
           "delay_basis": "", "delay_n": 0, "actions": [], "note": ""}

    if change == ChangeType.NO_CHANGE.value:
        # 確認不變的信不進行動清單，但要留下紀錄（證明這封信已被處理過）。
        return {**out, "priority": "—", "reasons": ["供應商確認照原計畫，無需行動"],
                "note": "供應商確認照原計畫，無需行動"}
    if change == ChangeType.PULL_IN.value:
        # 提前交貨要處理的是倉容與付款，不是缺料。
        return {**out, "priority": "P3", "reasons": ["供應商提前交貨"],
                "note": "提前交貨：請確認倉容與付款排程"}

    eta = _d(record.get("new_eta")) or _d(po.get("committed_date"))
    need = _d(po.get("need_date"))
    if eta is None or need is None:
        return {**out, "priority": "待查",
                "reasons": ["缺少交期或需求日，無法計算預估缺料天數"]}

    available = bool(estimate and estimate.get("available"))
    delay = int(estimate["delay_days"]) if available else 0
    conservative = eta + timedelta(days=delay)
    gap = (conservative - need).days

    reasons: list[str] = []
    if available:
        pct = int(round(float(estimate["percentile"]) * 100))
        reasons.append(
            f"供應商說 {eta.isoformat()}；這家供應商過去{estimate['basis']}"
            f"（{estimate['n']} 筆）有 {pct}% 在說定日期後 {delay} 天內到，"
            f"保守到料日 {conservative.isoformat()}")
    else:
        why = (estimate or {}).get("reason", "沒有歷史收貨紀錄")
        reasons.append(f"{why}；暫以供應商說的日期 {eta.isoformat()} 計算")
    if gap > 0:
        reasons.append(f"比下游需求日 {need.isoformat()} 晚 {gap} 天，預估缺料")
    else:
        reasons.append(f"距下游需求日 {need.isoformat()} 尚有 {-gap} 天緩衝")
    if gap > 0 and po.get("downstream_scheduled"):
        reasons.append("下游已排定產能或已對客戶承諾，缺料會連動整串排程")
    if gap > 0 and material.get("is_bottleneck"):
        reasons.append("瓶頸料，延誤難以補回")

    tight = int(triage_cfg["tight_buffer_days"])
    priority = _tier(gap, po, material, tight)
    if priority == "P2" and gap <= 0:
        reasons.append(f"單一來源且緩衝只剩 {-gap} 天（門檻 {tight} 天）")

    # 供應商說會延但沒給日期：上面只能拿原承諾日算，缺料天數必然被低估。
    # 這種單最需要企劃立刻追日期，至少排 P2，不能因為「看似有緩衝」而被放掉。
    if change == ChangeType.DELAY.value and _d(record.get("new_eta")) is None:
        reasons.insert(0, "供應商表示會延遲但未給新日期；以下以原承諾日估計，實際可能更晚")
        if priority == "P3":
            priority = "P2"

    return {**out, "priority": priority, "gap_days": gap,
            "conservative_eta": conservative.isoformat(),
            "delay_days_est": delay,
            "delay_basis": estimate["basis"] if available else "",
            "delay_n": int(estimate["n"]) if available else 0,
            "reasons": reasons,
            "actions": suggest_actions(priority, gap, record, po, material)}

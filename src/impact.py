# -*- coding: utf-8 -*-
"""
影響評估：10 條領域規則。

===========================  這是本專案的核心  ===========================
抽取（把信變成結構化資料）是通用能力，任何人接個 LLM 都做得出來。
**判斷（這個變更到底痛不痛、今天要不要處理）才是供應鏈專業。**

下面每一條規則都對應一個實務問題。我把判斷邏輯攤開寫，是因為：
  1. 生管有權質疑每一條規則，並要求調整權重 —— 黑箱不會被信任。
  2. 規則寫錯了要能被指出來。這比模型準度重要。
  3. 未來累積了真實的「預警 vs 實際結果」資料後，
     這張評分卡就是機器學習模型的 baseline。沒有 baseline 的模型沒有意義。

每條規則回傳 0.0~1.0 的分數（越高越該處理）與一句給生管看的理由。
=========================================================================
"""
from __future__ import annotations

from datetime import date, datetime

from domain import ChangeType, CommitmentStrength

Rule = tuple[float, str]  # (score, 人話理由)


def _d(v) -> date | None:
    """
    寬鬆的日期正規化。

    來源可能是 ISO 字串、datetime.date、或 pandas Timestamp
    （UI 就地重算時就是 Timestamp）。datetime 與 Timestamp 都是 date 的子類，
    但相減會得到 timedelta 以外的型別錯誤，所以必須先降階成純 date。
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


def _band(value: float, bands: list[tuple[float, float]]) -> float:
    """bands = [(上界, 分數), ...]，由小到大；超過最後一個上界則取最後的分數。"""
    for upper, score in bands:
        if value <= upper:
            return score
    return bands[-1][1]


# ---------------------------------------------------------------------------
# 規則 1：緩衝天數
# ---------------------------------------------------------------------------
def rule_buffer_days(ctx: dict) -> Rule:
    """
    延遲本身不痛，「來不來得及」才痛。

    緩衝天數 = 下游需求日 − 新的預計到料日。
    這是第一順位規則（權重最高），因為它直接回答生管唯一真正在意的問題：
    這批料會不會趕不上。一張延了 30 天但緩衝有 45 天的單，
    遠不如一張只延 3 天但緩衝已經歸零的單來得急。
    """
    eta = ctx["effective_eta"]
    need = _d(ctx["po"].get("need_date"))
    if not eta or not need:
        return 0.5, "缺少新交期或需求日，無法計算緩衝天數（暫給中性分數）"
    buf = (need - eta).days
    score = _band(float(buf), [(-1, 1.00), (3, 0.85), (7, 0.60),
                               (14, 0.35), (30, 0.15), (float("inf"), 0.05)])
    if buf < 0:
        return score, f"已來不及：新交期比下游需求日晚 {abs(buf)} 天"
    return score, f"緩衝僅剩 {buf} 天" if buf <= 7 else f"緩衝尚有 {buf} 天"


# ---------------------------------------------------------------------------
# 規則 2：延遲幅度
# ---------------------------------------------------------------------------
def rule_delay_magnitude(ctx: dict) -> Rule:
    """
    延遲天數的絕對量級。

    權重刻意壓低（10）。這是很多人做這類工具會犯的錯 ——
    直接照延遲天數排序。但延 45 天的單如果下游根本還沒排，
    優先級低於延 5 天卻卡住已排定產能的單。
    延遲天數是必要資訊，不是主要判準。
    """
    eta, committed = ctx["effective_eta"], _d(ctx["po"].get("committed_date"))
    if not eta or not committed:
        return 0.3, "無法計算延遲天數"
    delay = (eta - committed).days
    if delay <= 0:
        return 0.0, "未較原承諾日延後"
    score = _band(float(delay), [(3, 0.20), (7, 0.40), (14, 0.60),
                                 (30, 0.80), (float("inf"), 1.00)])
    return score, f"較原承諾日延後 {delay} 天"


# ---------------------------------------------------------------------------
# 規則 3：單一來源
# ---------------------------------------------------------------------------
def rule_single_source(ctx: dict) -> Rule:
    """
    有沒有已認證的第二來源。

    這條規則決定「還有沒有牌可打」。有二源 = 可以轉單，是商務問題；
    沒有二源 = 只能等，是無解問題。兩者的處理方式完全不同，
    生管看到清單時第一個想知道的就是這件事。

    注意：二源 ≠ 替代料（見規則 10）。
    二源是同一顆料的另一家合格供應商，可直接轉單；
    替代料是不同料號，通常要跑工程變更流程。實務上差很多。
    """
    if ctx["material"].get("has_qualified_second_source"):
        return 0.15, "有已認證二源，可評估轉單"
    return 1.00, "單一來源，無合格二源可轉單"


# ---------------------------------------------------------------------------
# 規則 4：下游已排定
# ---------------------------------------------------------------------------
def rule_downstream_scheduled(ctx: dict) -> Rule:
    """
    下游是否已排定產能或已對客戶承諾。

    這是影響「代價放大倍數」的關鍵旗標。一旦封測產能排好、
    或業務已經對客戶報了交期，上游延一天要動的就不只這一張單 ——
    後面整串排程、客戶溝通、甚至違約條款都會被牽動。
    """
    if ctx["po"].get("downstream_scheduled"):
        return 1.00, "下游已排定產能或已對客戶承諾，變更會連動整串排程"
    return 0.20, "下游尚未排定，調整空間較大"


# ---------------------------------------------------------------------------
# 規則 5：料的關鍵性
# ---------------------------------------------------------------------------
def rule_material_criticality(ctx: dict) -> Rule:
    """
    瓶頸料與長前置期料補不回來。

    LT 90 天的晶圓延誤，不可能靠加班或急件救回；
    LT 20 天的料還有機會。這條規則讓清單自動把「補不回來的」推到前面。
    """
    m = ctx["material"]
    base = {"high": 1.00, "medium": 0.55, "low": 0.20}.get(str(m.get("criticality")), 0.4)
    lt = int(m.get("std_lead_time_days") or 0)
    if m.get("is_bottleneck"):
        return max(base, 0.9), f"瓶頸料，標準前置期 {lt} 天，延誤難以補回"
    return base, f"標準前置期 {lt} 天"


# ---------------------------------------------------------------------------
# 規則 6：承諾強度  ← 本專案最重要的一條
# ---------------------------------------------------------------------------
def rule_commitment_strength(ctx: dict) -> Rule:
    """
    供應商到底有沒有承諾。

    「Revised ETA 10/30, confirmed」和「大概月底吧，我再跟你確認」
    在資料表裡都會變成一個日期，但它們的可信度天差地遠。

    這條規則的方向是**反直覺但正確**的：越不確定，分數越高（越該處理）。
    因為不確定的案子才需要生管打電話去逼一個確切日期；
    已經確認的案子反而只要照著調整排程就好。

    對應的守門機制在 pipeline：非 confirmed 的一律不覆寫系統承諾日，
    強制人工確認。這是整個工具最重要的安全設計。
    """
    s = ctx["record"].get("commitment_strength")
    if s == CommitmentStrength.CONFIRMED.value:
        return 0.25, "供應商已明確承諾此日期"
    if s == CommitmentStrength.ESTIMATED.value:
        return 0.65, "供應商僅給暫估日期，尚未鎖定，需再確認"
    if s == CommitmentStrength.INTENT_ONLY.value:
        return 1.00, "供應商僅表達意向、未做承諾，此日期不可作為排程依據"
    return 0.50, "信中未提供明確新日期"


# ---------------------------------------------------------------------------
# 規則 7：累犯
# ---------------------------------------------------------------------------
def rule_reschedule_count(ctx: dict) -> Rule:
    """
    這張單已經改期幾次。

    實務直覺：第三次跟你改期的供應商，這次給的日期一樣不能信。
    改期次數是「這條供應鏈失控程度」最直接的指標，
    而且它是 ERP 裡現成就有的欄位，不需要額外收集。
    """
    n = int(ctx["po"].get("reschedule_count") or 0)
    score = _band(float(n), [(0, 0.10), (1, 0.40), (2, 0.70), (float("inf"), 1.00)])
    if n == 0:
        return score, "此單首次變更"
    return score, f"此單已第 {n + 1} 次變更，供應商承諾可信度存疑"


# ---------------------------------------------------------------------------
# 規則 8：延遲量佔比
# ---------------------------------------------------------------------------
def rule_delay_share(ctx: dict) -> Rule:
    """
    這批延遲的數量佔該料號當期需求的比例。

    延 10% 的量可以用安全庫存吸收；延掉當期需求的 90%，
    就是實質斷料。同樣一張延遲通知，量的佔比決定它是雜訊還是災難。
    """
    share = float(ctx["po"].get("share_of_period_demand") or 0.5)
    pct = int(round(share * 100))
    return min(1.0, max(0.0, share)), f"影響數量約佔該料號當期需求 {pct}%"


# ---------------------------------------------------------------------------
# 規則 9：通知時機
# ---------------------------------------------------------------------------
def rule_notice_lead_time(ctx: dict) -> Rule:
    """
    距離原承諾日還剩幾天才通知。

    提早三週講，還有時間找替代方案、跟客戶協調；
    到期前一天才講，等於直接宣告開天窗。
    這條規則也順帶暴露了供應商的溝通品質 ——
    長期累積下來，是供應商評鑑的實質證據。
    """
    committed = _d(ctx["po"].get("committed_date"))
    received = _d(ctx["record"].get("received_at"))
    if not committed or not received:
        return 0.4, "無法判斷通知時機"
    days = (committed - received).days
    score = _band(float(days), [(3, 1.00), (7, 0.75), (14, 0.45),
                                (30, 0.20), (float("inf"), 0.10)])
    if days < 0:
        return 1.00, f"原承諾日已過 {abs(days)} 天才通知"
    return score, f"距原承諾日僅剩 {days} 天才通知" if days <= 7 else f"提前 {days} 天通知"


# ---------------------------------------------------------------------------
# 規則 10：可替代性
# ---------------------------------------------------------------------------
def rule_substitutability(ctx: dict) -> Rule:
    """
    有沒有功能可互換的替代料。

    權重最低（3），因為替代料通常要跑工程變更與客戶認可，
    緩不濟急。它是「最後一張牌」而非日常手段 ——
    這也是為什麼它的權重遠低於規則 3 的二源。
    """
    alt = str(ctx["material"].get("alt_material_id") or "").strip()
    if alt:
        return 0.20, f"有替代料 {alt} 可評估（需工程放行）"
    return 1.00, "無登錄替代料"


RULES = {
    "buffer_days": rule_buffer_days,
    "delay_magnitude": rule_delay_magnitude,
    "single_source": rule_single_source,
    "downstream_scheduled": rule_downstream_scheduled,
    "material_criticality": rule_material_criticality,
    "commitment_strength": rule_commitment_strength,
    "reschedule_count": rule_reschedule_count,
    "delay_share": rule_delay_share,
    "notice_lead_time": rule_notice_lead_time,
    "substitutability": rule_substitutability,
}


def evaluate(record: dict, po: dict, material: dict, supplier: dict,
             weights: dict, thresholds: dict) -> dict:
    """對一筆已對位的變更做完整影響評估。"""
    eta = _d(record.get("new_eta")) or _d(po.get("committed_date"))
    ctx = {"record": record, "po": po, "material": material,
           "supplier": supplier, "effective_eta": eta}

    change = record.get("change_type")

    details = []
    total_w = sum(float(weights.get(k, 0)) for k in RULES)
    weighted = 0.0
    for name, fn in RULES.items():
        w = float(weights.get(name, 0))
        score, why = fn(ctx)
        weighted += score * w
        details.append({"rule": name, "score": round(score, 3),
                        "weight": w, "contribution": round(score * w, 2),
                        "explain": why})

    impact = (weighted / total_w * 100) if total_w else 0.0

    # -------- 變更類型的特別處理 --------
    if change == ChangeType.NO_CHANGE.value:
        # 確認不變的信不進入行動清單，但要留下紀錄（證明這封信已被處理過）。
        impact, note = 0.0, "供應商確認照原計畫，無需行動"
    elif change == ChangeType.PULL_IN.value:
        # 提前交貨要處理的是倉容與付款，不是缺料。
        # 本版以「上限 40 分」簡化，屬已知限制，見 docs/設計決策.md。
        impact, note = min(impact, 40.0), "提前交貨：請確認倉容與付款排程"
    else:
        note = ""

    p1, p2 = float(thresholds.get("P1", 70)), float(thresholds.get("P2", 45))
    priority = "P1" if impact >= p1 else ("P2" if impact >= p2 else "P3")
    if change == ChangeType.NO_CHANGE.value:
        priority = "—"

    # 取貢獻度最高的三條規則當作「為什麼是這個優先級」的說明
    top = sorted([d for d in details if d["contribution"] > 0],
                 key=lambda d: d["contribution"], reverse=True)[:3]

    return {
        "impact_score": round(impact, 1),
        "priority": priority,
        "rule_details": details,
        "top_reasons": [d["explain"] for d in top],
        "note": note,
    }

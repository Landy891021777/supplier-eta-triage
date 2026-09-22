# -*- coding: utf-8 -*-
"""
交期風險分級：預估缺料天數 ＋ P1/P2/P3 ＋ 建議動作。

===========================  為什麼沒有權重  ===========================
舊版用十條規則各給 0~1 分、再依權重加成 0~100 分。那組權重是人手填的，
拿去「校準」時用的又是自己產生的歷史資料，等於用答案驗答案
（見 docs/設計決策.md 決策 16）。

這一版只問物料企劃真正在意的一件事：這張單到料時，需求日已經過了幾天？

    預估缺料天數 = 保守到料日 + 收貨處理天數 − 下游需求日
    保守到料日   = 供應商說的日期 + 這家供應商過去「說定後仍延遲」的天數
    收貨處理天數 = 料到廠後、進料檢驗等到可以投產還要幾天
                  （仿 SAP 料號主檔 MARC-WEBAZ，見 src/planner_settings.py）

那是兩個日期相減，可以在會議上逐項驗算，不是分數。
分級只用「有沒有缺料」與三個事實旗標，沒有任何可調權重。

分級規則（依判斷順序）：
  - 供應商確認不變 → 「—」，不進行動清單。
  - 提前交貨（PULL_IN）：提前後的日期仍不晚於需求日 → P3，只提醒倉容與付款；
    提前後的日期還是晚於需求日（等於根本沒解決缺料），不算「提前」，
    照下面的斷料分級正常走。
  - 對不到可用新日期、且不是已判斷為「延遲」的信（例如變更內容判斷不出來、
    change_type 是 unknown）→「待查」，不拿原承諾日硬套，避免假裝算得出
    缺料天數；只有明確判斷為延遲時，才允許用原承諾日頂替新日期去估
    （並把估出來的結果至少頂到 P2，因為那個天數注定被低估）。
  - 其餘才是：有沒有缺料（gap_days > 0）與三個事實旗標（下游已排產能、
    瓶頸料、單一來源）決定 P1／P2／P3，沒有任何可調權重。
=====================================================================

領域假設：`need_date` 視為 MRP 的淨需求日（已扣庫存與在途），
因此不另外建庫存表。若接的是未扣庫存的需求日，缺料天數會被高估。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from domain import ChangeType

# 排序用：分級由前到後，同級內再依預估缺料天數由大到小。
PRIORITY_RANK = {"P1": 0, "P2": 1, "P3": 2, "待查": 3, "—": 4}

# 與 src/supplier_stats.py 的 estimate_delay() 耦合：改期單樣本不足、
# 退回全部單估計時，basis 一定回這個字串。這裡只是把它換成企劃看得懂的
# 說法，不是另外判斷退回邏輯——退回邏輯的唯一真相在 supplier_stats.py。
_HISTORY_FALLBACK_BASIS = "全部單（改期單樣本不足）"


def _missing(v) -> bool:
    """
    判斷是否為空值，對 pandas.NA 安全。

    NaN 的 `!=` 比較會直接得到 True（NaN 不等於自己），但 pd.NA 的
    `!=` 走三態邏輯，回傳的還是 pd.NA，`bool(pd.NA)` 會丟 TypeError。
    ERP／CSV 讀進來的空格常常是 pd.NA，不是 NaN，兩種都要接得住。
    """
    if v is None:
        return True
    try:
        return bool(v != v)
    except TypeError:
        return True


def _d(v) -> date | None:
    """
    寬鬆的日期正規化。

    來源可能是 ISO 字串、datetime.date、或 pandas Timestamp。
    Timestamp 與 datetime 都是 date 的子類，但直接相減型別會出錯，
    所以必須先降階成純 date。
    """
    if _missing(v):
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
    if _missing(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none", "nat", "null", "-"} else s


def _flag(v) -> bool:
    """
    把 ERP／CSV 讀進來的旗標欄位正規化成布林。

    兩個真實會發生的坑：
      1. 空值（None／NaN／pd.NA）代表「未填」，但 bool(NaN) 是 True，
         會被誤判成旗標成立。
      2. CSV 讀進來的布林常常是字串。bool("False") 也是 True
         （非空字串），會把「沒有下游排程」誤判成「有」。
    兩種都要在這裡攔下來，不能指望呼叫端記得處理。
    """
    if _missing(v):
        return False
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(v)


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
    # 領域假設：已認證二源可用轉單吸收偏緊的緩衝，所以緩衝緊只對單一來源升級。
    critical = _flag(po.get("downstream_scheduled")) or _flag(material.get("is_bottleneck"))
    single_source = not _flag(material.get("has_qualified_second_source"))
    if gap > 0:
        return "P1" if critical else "P2"
    if single_source and gap >= -tight_days:
        return "P2"
    return "P3"


def suggest_actions(priority: str, gap: int, record: dict, po: dict,
                    material: dict, gr_days: int = 0) -> list[str]:
    """
    給物料企劃「自己能做」的下一步。

    詢價與下單是採購的工作、驗證替代料是品保的工作，所以動作寫成
    「與採購確認」「請品保確認」，不寫成企劃自己去做。
    成本與可行性資料庫裡沒有，因此不寫。

    順序：追日期、通知生管是企劃今天就能做的事，排最前面；缺的天數如果在
    收貨處理天數以內，請 IQC 優先安排進料檢驗就趕得上，這是企劃自己救得回
    來的動作，排在通知生管之後、催貨等要跟採購對過才能開口的手段之前。
    只要分級是 P1／P2，一定至少有一個動作——企劃不該看到「有風險卻沒事
    可做」的單。
    """
    if priority not in ("P1", "P2"):
        return []
    acts: list[str] = []
    has_new = _d(record.get("new_eta")) is not None

    if not has_new or record.get("commitment_strength") != "confirmed":
        why = "（信中未給新日期）" if not has_new else "（目前日期尚未確認）"
        acts.append(f"先向供應商追一個可承諾的確切日期{why}")
    if _flag(po.get("downstream_scheduled")):
        acts.append("通知生管：這張單可能缺料，需確認下游排程")
    if 0 < gap <= gr_days:
        acts.append(f"請品保（IQC）優先安排進料檢驗：收貨處理 {gr_days} 天若能縮短 "
                    f"{gap} 天就趕得上")
    if gap > 0:
        acts.append("可評估的手段：催貨、拉貨、分批交、空運"
                    "（成本與可行性需與採購、供應商確認）")
    elif has_new:
        if gap == 0:
            acts.append("沒有緩衝：向供應商確認出貨進度，到期前再追一次")
        else:
            acts.append(f"緩衝只剩 {-gap} 天：向供應商確認出貨進度，到期前再追一次")
    if _flag(material.get("has_qualified_second_source")):
        acts.append("與採購確認能否向已認證二源詢價備案")
    else:
        acts.append("單一來源，無已認證二源可轉單，只能追供應商")
    alt = _clean_str(material.get("alt_material_id"))
    if alt:
        acts.append(f"替代料 {alt}：請品保確認是否已通過驗證，或能否走特採；"
                    "未確認前不可視為可用")
    return acts


def evaluate(record: dict, po: dict, material: dict, triage_cfg: dict,
             estimate: dict | None) -> dict:
    """
    對一筆已對位的變更算預估缺料天數、分級與建議動作。

    estimate 是 supplier_stats.estimate_delay 的結果（由呼叫端依承諾強度
    選好百分位後傳入），本函式不讀資料庫，方便單獨測試。
    """
    change = record.get("change_type")
    out = {"gap_days": None, "conservative_eta": None, "available_date": None,
           "delay_days_est": 0, "delay_basis": "", "delay_n": 0, "actions": [],
           "note": ""}

    if change == ChangeType.NO_CHANGE.value:
        # 確認不變的信不進行動清單，但要留下紀錄（證明這封信已被處理過）。
        return {**out, "priority": "—", "reasons": ["供應商確認照原計畫，無需行動"],
                "note": "供應商確認照原計畫，無需行動"}

    pull_in_late = False
    if change == ChangeType.PULL_IN.value:
        eta_pull = _d(record.get("new_eta"))
        need_pull = _d(po.get("need_date"))
        if eta_pull is None or need_pull is None or eta_pull <= need_pull:
            # 提前交貨要處理的是倉容與付款，不是缺料。
            return {**out, "priority": "P3", "reasons": ["供應商提前交貨"],
                    "note": "提前交貨：請確認倉容與付款排程"}
        # 說是「提前」，提前後的日期卻還是晚於需求日：根本沒解決缺料，
        # 不能因為標籤是 PULL_IN 就放進倉容那條輕鬆路徑，要照斷料分級走。
        pull_in_late = True

    new_eta_d = _d(record.get("new_eta"))
    has_new = new_eta_d is not None

    if change != ChangeType.DELAY.value and not has_new:
        # 只有明確判斷為「延遲」時，才敢拿原承諾日頂替新日期去估——那是
        # 「供應商說會延遲，只是沒給新日期」的已知情境。其他 change_type
        # （含 unknown）沒有新日期時，代表信根本沒解析出變更內容，
        # 不能假裝已經算得出缺料天數。
        return {**out, "priority": "待查",
                "reasons": ["信中對到採購單，但讀不出新日期或變更內容，需人工看信"]}

    eta = new_eta_d or _d(po.get("committed_date"))
    need = _d(po.get("need_date"))
    if eta is None or need is None:
        return {**out, "priority": "待查",
                "reasons": ["缺少交期或需求日，無法計算預估缺料天數"]}

    available = bool(estimate and estimate.get("available"))
    delay = int(estimate["delay_days"]) if available else 0
    conservative = eta + timedelta(days=delay)

    # 收貨處理天數（≈ SAP MARC-WEBAZ）：料到廠不代表能投產，還要幾天檢驗、
    # 入庫，光阻甚至要回溫一晚。_missing 防的是 NaN——料號主檔沒維護這個
    # 欄位時，視為當天可用，不是讓 int() 直接炸掉。
    gr_raw = material.get("gr_processing_days")
    gr = 0 if _missing(gr_raw) else int(gr_raw)
    usable = conservative + timedelta(days=gr)
    gap = (usable - need).days

    reasons: list[str] = []
    if available:
        pct = int(round(float(estimate["percentile"]) * 100))
        who = f"供應商說 {eta.isoformat()}" if has_new else f"原承諾日 {eta.isoformat()}"
        if estimate["basis"] == _HISTORY_FALLBACK_BASIS:
            body = (f"這家供應商過去全部單共 {estimate['n']} 筆"
                    "（改期單不足，改用全部單）")
        else:
            body = f"這家供應商過去{estimate['basis']}（{estimate['n']} 筆）"
        rate = (f"有 {pct}% 準時或提前到" if delay == 0
                else f"有 {pct}% 在說定日期後 {delay} 天內到")
        reasons.append(f"{who}；{body}{rate}，保守到料日 {conservative.isoformat()}")
    else:
        why = (estimate or {}).get("reason", "沒有歷史收貨紀錄")
        basis_date = (f"供應商說的日期 {eta.isoformat()}" if has_new
                      else f"原承諾日 {eta.isoformat()}")
        reasons.append(f"{why}；暫以{basis_date}計算")

    if gr > 0:
        reasons.append(f"加上收貨處理 {gr} 天（{material.get('gr_source') or '料號主檔'}），"
                        f"可投產日 {usable.isoformat()}")

    if pull_in_late:
        reasons.append(f"供應商已提前到 {eta.isoformat()}，但仍晚於下游需求日")

    if gap > 0:
        if gr > 0:
            reasons.append(f"可投產日比下游需求日 {need.isoformat()} 晚 {gap} 天，預估缺料")
        else:
            reasons.append(f"比下游需求日 {need.isoformat()} 晚 {gap} 天，預估缺料")
    elif gap == 0:
        # 剛好同一天到，沒有緩衝可言。gr > 0 時「可投產日」已經含進料檢驗，
        # gr == 0 時還沒算進料檢驗要花的時間，兩種措辭不能混用。
        if gr > 0:
            reasons.append("可投產日與下游需求日同一天，沒有緩衝")
        else:
            reasons.append("與下游需求日同一天到，沒有緩衝（未含進料檢驗時間）")
    elif has_new:
        if gr > 0:
            reasons.append(f"以可投產日計，距下游需求日 {need.isoformat()} 尚有 {-gap} 天緩衝")
        else:
            reasons.append(f"距下游需求日 {need.isoformat()} 尚有 {-gap} 天緩衝")
    else:
        # 沒有新日期時這個緩衝是拿原承諾日算出來的，不是供應商剛說的話，
        # 不能讓企劃誤以為真的還有這麼多餘裕。
        if gr > 0:
            reasons.append(
                f"以可投產日計，距下游需求日 {need.isoformat()} 尚有 {-gap} 天緩衝"
                "（以原承諾日計，新日期未知，不能當真）")
        else:
            reasons.append(
                f"距下游需求日 {need.isoformat()} 尚有 {-gap} 天緩衝"
                "（以原承諾日計，新日期未知，不能當真）")

    if gap > 0 and _flag(po.get("downstream_scheduled")):
        reasons.append("下游已排定產能或已對客戶承諾，缺料會連動整串排程")
    if gap > 0 and _flag(material.get("is_bottleneck")):
        reasons.append("瓶頸料，延誤難以補回")

    tight = int(triage_cfg["tight_buffer_days"])
    priority = _tier(gap, po, material, tight)
    if priority == "P2" and gap <= 0:
        if gap == 0:
            reasons.append(f"單一來源且沒有緩衝（門檻 {tight} 天）")
        else:
            reasons.append(f"單一來源且緩衝只剩 {-gap} 天（門檻 {tight} 天）")

    # 供應商說會延但沒給日期：上面只能拿原承諾日算，缺料天數必然被低估。
    # 這種單最需要企劃立刻追日期，至少排 P2，不能因為「看似有緩衝」而被放掉。
    if change == ChangeType.DELAY.value and not has_new:
        reasons.insert(0, "供應商表示會延遲但未給新日期；以下以原承諾日估計，實際可能更晚")
        if priority == "P3":
            priority = "P2"

    return {**out, "priority": priority, "gap_days": gap,
            "conservative_eta": conservative.isoformat(),
            "available_date": usable.isoformat(),
            "delay_days_est": delay,
            "delay_basis": estimate["basis"] if available else "",
            "delay_n": int(estimate["n"]) if available else 0,
            "reasons": reasons,
            "actions": suggest_actions(priority, gap, record, po, material, gr)}

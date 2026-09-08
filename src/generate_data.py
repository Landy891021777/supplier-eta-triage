# -*- coding: utf-8 -*-
"""
合成資料產生器。

===========================  請先讀這段  ===========================
本專案沒有使用任何真實資料。真實的 PO、交期與供應商往來屬營業秘密，
不可能出現在公開作品集裡。

但「因為沒資料所以隨便亂灑」是不負責任的做法。這支程式的定位是：
**把我的領域假設外顯化，寫成任何人都能逐條檢視、質疑、修改的程式碼。**

因此請把這支程式當成本專案的主要交付物之一來讀。
下面每一段 `# 領域假設：` 註解，都是我在晶圓廠做物料企劃時的實際觀察。
它們可能不完全適用於每一家公司 —— 這正是我希望被挑戰的地方。

評估上的誠實聲明：
    在合成資料上得到的任何數字，只能證明「程式邏輯自洽」，
    不能證明「在真實環境有效」。真實效能必須上線後以 A/B 驗證。
===================================================================
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

try:  # 允許以 `python src/generate_data.py` 或 `python -m src.generate_data` 執行
    from .domain import MaterialCategory, SupplierType
    from .handcrafted_emails import HANDCRAFTED
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from domain import MaterialCategory, SupplierType
    from handcrafted_emails import HANDCRAFTED

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
INBOX = DATA / "inbox"


def _load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# 供應商主檔
# ---------------------------------------------------------------------------
# 領域假設：供應商一律以代號表示，不使用任何真實公司名稱。
#   這不只是法務考量，也是實務習慣 —— 對外的分析報告不會直接掛供應商名字。
#
# 領域假設：不同類型供應商的「準交率」體質差很多。
#   晶圓代工的排程相對可預測（產能是長期鎖定的），
#   但載板/基板廠在缺料循環中的交期波動可以非常大 ——
#   ABF 載板缺貨期間，承諾日形同參考值。
SUPPLIER_SEED = [
    ("SUP-F01", "Foundry-Alpha",    SupplierType.FOUNDRY,   0.88),
    ("SUP-F02", "Foundry-Bravo",    SupplierType.FOUNDRY,   0.93),
    ("SUP-F03", "Foundry-Charlie",  SupplierType.FOUNDRY,   0.74),  # 體質最差，累犯多
    ("SUP-F04", "Foundry-Delta",    SupplierType.FOUNDRY,   0.90),
    ("SUP-M01", "Mask-Echo",        SupplierType.MASK_SHOP, 0.91),
    ("SUP-M02", "Mask-Foxtrot",     SupplierType.MASK_SHOP, 0.85),
    ("SUP-S01", "Substrate-Golf",   SupplierType.SUBSTRATE, 0.69),  # 載板體質差
    ("SUP-S02", "Substrate-Hotel",  SupplierType.SUBSTRATE, 0.72),
    ("SUP-S03", "Substrate-India",  SupplierType.SUBSTRATE, 0.80),
    ("SUP-O01", "OSAT-Juliet",      SupplierType.OSAT,      0.87),
    ("SUP-O02", "OSAT-Kilo",        SupplierType.OSAT,      0.83),
    ("SUP-O03", "OSAT-Lima",        SupplierType.OSAT,      0.79),
]


def build_suppliers() -> list[dict]:
    rows = []
    for sid, name, stype, otd in SUPPLIER_SEED:
        rows.append({
            "supplier_id": sid,
            "supplier_name": name,
            "supplier_type": stype.value,
            # 領域假設：歷史準交率是「過去 12 個月，實際到料日 <= 承諾日」的比例。
            #   這個欄位在真實環境應由 ERP 收貨紀錄回算，不該手動維護。
            "historical_otd_rate": otd,
            # 領域假設：窗口回覆品質差異很大。有些窗口報的日期就是可信，
            #   有些永遠報一個樂觀值再慢慢改。這個欄位在真實環境需要人工標註，
            #   本版僅作為「未來可擴充的訊號」示意，未納入影響評分。
            "contact_reliability": round(min(1.0, otd + random.uniform(-0.05, 0.05)), 2),
        })
    return rows


# ---------------------------------------------------------------------------
# 料號主檔
# ---------------------------------------------------------------------------
# 領域假設：料號的三個關鍵屬性決定「延遲有多痛」，而非延遲天數本身：
#   1. 標準前置期 (LT)：LT 越長，越補不回來。晶圓段動輒 2-3 個月。
#   2. 是否為瓶頸料：卡住它，後面一整串都動不了。
#   3. 有無已認證二源：沒有二源 = 只能等，有二源 = 還有牌可打。
#
# 領域假設（很重要）：「有替代料」和「有二源」是兩件事。
#   二源 = 同一顆料，另一家供應商也做，且已通過認證。
#   替代料 = 不同料號，但功能上可互換（通常需要工程放行）。
#   前者可以直接轉單，後者要跑變更流程。工具必須分開處理。
WAFER_NODES = ["N6", "N7", "N12", "N16", "N22"]
PRODUCT_CODES = ["XR3390", "KL2210", "MT8195", "AB7710", "CD4420",
                 "EF9930", "GH1180", "IJ6650", "KL7720", "MN3310"]


def build_materials(n: int) -> list[dict]:
    rows: list[dict] = []
    # 先放進手寫案例會用到的固定料號，確保 demo 情境可重現
    fixed = [
        ("WF-N6-XR3390",   MaterialCategory.WAFER,     92, True,  False, ""),
        ("WF-N7-KL2210",   MaterialCategory.WAFER,     85, False, True,  "WF-N7-KL2211"),
        ("WF-N7-KL2211",   MaterialCategory.WAFER,     85, False, True,  "WF-N7-KL2210"),
        ("MSK-N6-XR3390",  MaterialCategory.MASK,      21, True,  False, ""),
        ("SUB-FCCSP-1088", MaterialCategory.SUBSTRATE, 70, True,  False, ""),
        ("SUB-FCCSP-1090", MaterialCategory.SUBSTRATE, 65, False, True,  "SUB-FCCSP-1091"),
        ("SUB-FCCSP-1091", MaterialCategory.SUBSTRATE, 65, False, True,  "SUB-FCCSP-1090"),
    ]
    for mid, cat, lt, bottleneck, second_src, alt in fixed:
        rows.append({
            "material_id": mid,
            "category": cat.value,
            "std_lead_time_days": lt,
            "is_bottleneck": bottleneck,
            "has_qualified_second_source": second_src,
            "alt_material_id": alt,
            "criticality": "high" if bottleneck else ("medium" if lt >= 70 else "low"),
        })

    while len(rows) < n:
        cat = random.choices(
            [MaterialCategory.WAFER, MaterialCategory.MASK,
             MaterialCategory.SUBSTRATE, MaterialCategory.ASSEMBLY],
            weights=[0.55, 0.10, 0.20, 0.15],  # 範圍鎖 wafer 段，故 WAFER 佔多數
        )[0]
        if cat is MaterialCategory.WAFER:
            mid = f"WF-{random.choice(WAFER_NODES)}-{random.choice(PRODUCT_CODES)}"
            lt = random.randint(70, 110)   # 領域假設：晶圓段 LT 約 2.5-3.5 個月
        elif cat is MaterialCategory.MASK:
            mid = f"MSK-{random.choice(WAFER_NODES)}-{random.choice(PRODUCT_CODES)}"
            lt = random.randint(14, 35)
        elif cat is MaterialCategory.SUBSTRATE:
            mid = f"SUB-FCCSP-{random.randint(1000, 1999)}"
            lt = random.randint(45, 90)    # 領域假設：載板 LT 長且在缺料期會爆增
        else:
            mid = f"ASM-{random.choice(PRODUCT_CODES)}-{random.randint(10, 99)}"
            lt = random.randint(20, 40)
        if any(r["material_id"] == mid for r in rows):
            continue
        # 領域假設：約 1/4 的料是瓶頸料。瓶頸料通常也就是沒有二源的那些
        #   —— 因為有二源的料，本來就不太會變成瓶頸。這個相關性是刻意做進去的。
        bottleneck = random.random() < 0.25
        second_src = (not bottleneck) and random.random() < 0.55
        rows.append({
            "material_id": mid,
            "category": cat.value,
            "std_lead_time_days": lt,
            "is_bottleneck": bottleneck,
            "has_qualified_second_source": second_src,
            "alt_material_id": "",
            "criticality": "high" if bottleneck else ("medium" if lt >= 70 else "low"),
        })
    return rows


# ---------------------------------------------------------------------------
# PO 主檔
# ---------------------------------------------------------------------------
# 領域假設：need_date（下游需求日）不等於 committed_date（供應商承諾日）。
#   兩者的差距就是「緩衝天數」，而緩衝天數才是判斷延遲痛不痛的第一順位。
#   實務上這個緩衝會被壓縮：越接近旺季、越缺料，buffer 越薄。
#
# 領域假設：downstream_scheduled（下游已排定）是關鍵旗標。
#   一旦封測產能已經排好、或業務已經對客戶報了交期，
#   上游延一天的代價會放大很多倍 —— 因為要動的不只這一張單。
FIXED_POS = [
    # (po_no, material_id, supplier_id, qty, committed, need_date,
    #  downstream_scheduled, reschedule_count, share_of_demand)
    ("PO-2026-04417", "WF-N6-XR3390",   "SUP-F01", 3000, "2026-10-15", "2026-10-22", True,  2, 0.80),
    ("PO-2026-04452", "WF-N7-KL2210",   "SUP-F01", 2000, "2026-10-20", "2026-11-10", False, 0, 0.35),
    ("PO-2026-04390", "WF-N7-KL2210",   "SUP-F02", 2500, "2026-09-25", "2026-10-05", True,  1, 0.55),
    ("PO-2026-04391", "WF-N7-KL2210",   "SUP-F02", 2500, "2026-10-02", "2026-10-30", False, 0, 0.45),
    ("PO-2026-04205", "SUB-FCCSP-1088", "SUP-S01", 8000, "2026-09-10", "2026-09-18", True,  3, 0.95),
    ("PO-2026-04501", "WF-N12-MT8195",  "SUP-F01", 1500, "2026-09-30", "2026-10-25", False, 0, 0.30),
    ("PO-2026-04333", "WF-N6-XR3390",   "SUP-F03", 2200, "2026-09-20", "2026-09-28", True,  1, 0.60),
    ("PO-2026-04466", "WF-N7-KL2211",   "SUP-F02", 1800, "2026-09-30", "2026-10-20", False, 0, 0.40),
    ("PO-2026-04120", "MSK-N6-XR3390",  "SUP-M01", 1,    "2026-09-12", "2026-09-16", True,  0, 1.00),
    ("PO-2026-04277", "WF-N16-AB7710",  "SUP-F03", 4000, "2026-10-05", "2026-10-28", False, 0, 0.50),
    ("PO-2026-04278", "WF-N16-AB7710",  "SUP-F03", 4000, "2026-10-10", "2026-10-18", True,  2, 0.70),
    ("PO-2026-04279", "WF-N22-CD4420",  "SUP-F03", 6000, "2026-10-12", "2026-11-05", False, 3, 0.85),
    ("PO-2026-04188", "SUB-FCCSP-1090", "SUP-S02", 5000, "2026-09-30", "2026-10-08", True,  1, 0.75),
]


def build_pos(materials: list[dict], suppliers: list[dict], n: int, as_of: date) -> list[dict]:
    by_id = {m["material_id"]: m for m in materials}
    rows: list[dict] = []

    for (po, mid, sup, qty, committed, need, sched, resched, share) in FIXED_POS:
        if mid not in by_id:  # 固定料號若不在主檔則補上，確保 demo 一定跑得起來
            by_id[mid] = {
                "material_id": mid, "category": MaterialCategory.WAFER.value,
                "std_lead_time_days": 90, "is_bottleneck": True,
                "has_qualified_second_source": False, "alt_material_id": "",
                "criticality": "high",
            }
            materials.append(by_id[mid])
        rows.append({
            "po_no": po, "material_id": mid, "supplier_id": sup, "qty": qty,
            "committed_date": committed, "need_date": need,
            "downstream_scheduled": sched, "reschedule_count": resched,
            "share_of_period_demand": share,
            "po_created_date": (as_of - timedelta(days=random.randint(30, 120))).isoformat(),
        })

    seq = 4600
    while len(rows) < n:
        seq += random.randint(1, 4)
        m = random.choice(materials)
        # 領域假設：供應商類型要和料號類別對得起來 —— 載板不會去找光罩廠做。
        wanted = {
            MaterialCategory.WAFER.value: SupplierType.FOUNDRY.value,
            MaterialCategory.MASK.value: SupplierType.MASK_SHOP.value,
            MaterialCategory.SUBSTRATE.value: SupplierType.SUBSTRATE.value,
            MaterialCategory.ASSEMBLY.value: SupplierType.OSAT.value,
        }[m["category"]]
        cands = [s for s in suppliers if s["supplier_type"] == wanted]
        sup = random.choice(cands)["supplier_id"]

        committed = as_of + timedelta(days=random.randint(-10, 100))
        # 領域假設：緩衝天數多半落在 0-30 天；瓶頸料的緩衝通常更薄，
        #   因為它們排程壓得緊，沒有多餘空間。這個相關性刻意做進去。
        buffer_days = random.randint(0, 12) if m["is_bottleneck"] else random.randint(3, 35)
        rows.append({
            "po_no": f"PO-2026-{seq:05d}",
            "material_id": m["material_id"],
            "supplier_id": sup,
            "qty": random.choice([500, 1000, 1500, 2000, 3000, 5000, 8000]),
            "committed_date": committed.isoformat(),
            "need_date": (committed + timedelta(days=buffer_days)).isoformat(),
            # 領域假設：越接近交期，下游越可能已經排定。
            "downstream_scheduled": (committed - as_of).days < 30 and random.random() < 0.6,
            # 領域假設：改期次數呈長尾 —— 多數 PO 沒改過，少數改到爛。
            #   而且改過的更容易再改（這正是「累犯」規則存在的理由）。
            "reschedule_count": random.choices([0, 1, 2, 3, 4], weights=[0.55, 0.22, 0.13, 0.07, 0.03])[0],
            "share_of_period_demand": round(random.uniform(0.15, 1.0), 2),
            "po_created_date": (as_of - timedelta(days=random.randint(30, 150))).isoformat(),
        })
    return rows


# ---------------------------------------------------------------------------
# 信件產生
# ---------------------------------------------------------------------------
# 各種書寫風格。刻意做出「規則層抓得到」與「規則層抓不到」兩群，
# 對照實驗才有意義 —— 若全部都是格式化郵件，會得出「不需要 LLM」的假結論。
REASON_TEXT_EN = {
    "capacity": "capacity constraint at our fab",
    "yield": "a yield excursion",
    "upstream_shortage": "upstream raw material shortage",
    "logistics": "customs clearance delay",
    "customer_priority": "allocation adjustment",
    "internal_reschedule": "internal reschedule",
}
REASON_TEXT_ZH = {
    "capacity": "產能吃緊",
    "yield": "製程良率異常",
    "upstream_shortage": "上游材料短缺",
    "logistics": "運輸延誤",
    "customer_priority": "客戶排配調整",
    "internal_reschedule": "內部重排",
}


def _fmt(d: date, style: str) -> str:
    return {
        "iso": d.isoformat(),
        "slash": f"{d.month}/{d.day}",
        "dmy": d.strftime("%d-%b-%Y"),
        "zh": f"{d.month} 月 {d.day} 日",
    }[style]


def _email_formal(po, new_eta, reason, orig) -> tuple[str, str, str]:
    """格式化通知 —— 規則層應該要 100% 抓到。"""
    body = (
        "Dear Customer,\n\n"
        "Please note the following delivery schedule change:\n\n"
        f"PO Number    : {po}\n"
        f"Original ETA : {_fmt(orig, 'iso')}\n"
        f"Revised ETA  : {_fmt(new_eta, 'iso')}\n"
        f"Reason       : {REASON_TEXT_EN[reason]}\n\n"
        "The revised date is confirmed.\n\nBest regards,\nPlanning"
    )
    return "Delivery Schedule Change Notification", body, "confirmed"


def _email_semi(po, new_eta, reason, orig) -> tuple[str, str, str]:
    """半格式化 —— 日期格式雜，規則層需要多種 pattern 才抓得到。"""
    style = random.choice(["slash", "dmy", "zh"])
    if style == "zh":
        body = (
            "您好，\n\n"
            f"{po} 因{REASON_TEXT_ZH[reason]}，交期需要調整，\n"
            f"由原訂的 {_fmt(orig, 'zh')} 順延至 {_fmt(new_eta, 'zh')}。\n"
            "此日期已確認。\n\n業務部"
        )
    else:
        body = (
            "Hi,\n\n"
            f"Update on {po}: due to {REASON_TEXT_EN[reason]}, we need to move the "
            f"delivery from {_fmt(orig, style)} to {_fmt(new_eta, style)}.\n"
            "This is confirmed on our side.\n\nThanks"
        )
    return f"Re: {po} schedule update", body, "confirmed"


def _month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return nxt - timedelta(days=1)


def _email_narrative(po, new_eta, reason, orig, as_of: date) -> tuple[str, str, str, date]:
    """
    純敘述、相對日期、模糊措辭 —— 規則層基本上會失敗。
    這一群就是「為什麼需要 LLM」的證據來源。

    ★ 標準答案必須從信件文字可還原 ★

        本函式回傳「實際的正確日期」，而不是外部傳進來的 new_eta。

        原因是一個真實踩過的坑：早期版本讓信裡寫「往後抓個兩週」，
        但標準答案存的是精確天數（例如 10 天）。那個日期根本無法從
        文字還原 —— 等於在考一道沒有答案的題目，任何模型都會得零分，
        而那個零分會被誤讀成「LLM 不會算相對日期」。

        同理，承諾強度也必須由信中實際使用的措辭決定，
        不能隨機貼標籤。標準答案若與文字脫鉤，整個實驗就失去意義。

    這件事比它看起來重要：**評估資料的瑕疵會產生錯誤的技術結論**，
    而錯誤的技術結論會讓人做出錯誤的架構決定。
    """
    # 「下個月中／下個月底」是相對於「寫信當下」，不是相對於原承諾日 ——
    # 這是人講話的方式。因此只有在原承諾日離現在夠近時才用這種說法，
    # 否則推算出來的日期會早於原承諾日，語意不通。
    modes = ["weeks"]
    if 0 <= (orig - as_of).days <= 25:
        modes += ["mid_next_month", "end_next_month"]
    mode = random.choice(modes)
    if mode == "weeks":
        weeks = max(1, round((new_eta - orig).days / 7))
        actual = orig + timedelta(weeks=weeks)
        phrasing = random.choice([
            f"we may need roughly {weeks} more week(s) beyond the original date",
            f"大概要再往後 {weeks} 週左右",
        ])
    elif mode == "mid_next_month":
        actual = date(as_of.year + (as_of.month == 12), (as_of.month % 12) + 1, 15)
        phrasing = random.choice([
            "it will likely slip to around the middle of next month",
            "大概要到下個月中",
        ])
    else:
        actual = _month_end(date(as_of.year + (as_of.month == 12),
                                 (as_of.month % 12) + 1, 1))
        phrasing = random.choice([
            "we are looking at the end of next month at the earliest",
            "恐怕要到下個月底才有辦法",
        ])

    # 承諾強度由措辭決定，不是隨機指定。
    #   soft = 給了日期但明說沒鎖定 -> estimated
    #   hard = 明確表示無法承諾     -> intent_only
    if random.random() < 0.5:
        hedge = random.choice(["but this is not locked yet",
                               "pending final confirmation",
                               "這個日期還沒鎖定"])
        strength = "estimated"
    else:
        hedge = random.choice(["I cannot commit a firm date at this point",
                               "我沒辦法給你確定的日期，還要再跟工廠確認",
                               "we are unable to commit at this stage"])
        strength = "intent_only"

    body = (
        "Hi,\n\n"
        f"About {po} — because of {REASON_TEXT_EN[reason]}, {phrasing}. {hedge}.\n"
        "Sorry for the inconvenience.\n\nRgds"
    )
    return f"RE: {po}", body, strength, actual


def _email_no_change(po, orig) -> tuple[str, str, str]:
    """確認照原計畫 —— 陷阱組。誤判成延遲會把行動清單灌爆。"""
    body = (
        "Hi,\n\n"
        f"Just confirming {po} remains on schedule for {_fmt(orig, 'iso')}. "
        "No change needed.\n\nThanks"
    )
    return f"{po} - on track", body, "confirmed"


def build_emails(pos: list[dict], n: int, as_of: date) -> tuple[list[dict], list[dict]]:
    """回傳 (emails, ground_truth_records)。"""
    emails: list[dict] = []
    truth: list[dict] = []

    # 只針對「還沒到期」的 PO 發通知 —— 已過期的單不會再收到改期信
    open_pos = [p for p in pos
                if date.fromisoformat(p["committed_date"]) >= as_of
                and p["po_no"] not in {f[0] for f in FIXED_POS}]
    random.shuffle(open_pos)

    styles = (["formal"] * int(n * 0.35) + ["semi"] * int(n * 0.25)
              + ["narrative"] * int(n * 0.28) + ["no_change"] * int(n * 0.12))
    while len(styles) < n:
        styles.append("formal")
    random.shuffle(styles)

    for i, style in enumerate(styles[:min(n, len(open_pos))]):
        po = open_pos[i]
        orig = date.fromisoformat(po["committed_date"])
        reason = random.choices(
            list(REASON_TEXT_EN.keys()),
            # 領域假設：產能排擠與良率異常是最常見的兩大原因。
            weights=[0.34, 0.22, 0.16, 0.08, 0.12, 0.08],
        )[0]

        if style == "no_change":
            subj, body, strength = _email_no_change(po["po_no"], orig)
            new_eta, ctype = None, "no_change"
        else:
            delay = random.choices([3, 7, 10, 14, 21, 30, 45],
                                   weights=[.18, .22, .18, .17, .13, .08, .04])[0]
            new_eta_d = orig + timedelta(days=delay)
            if style == "narrative":
                # narrative 會依實際措辭重新推算日期並回傳，
                # 確保標準答案是「從信件文字可還原」的那個日期。
                subj, body, strength, new_eta_d = _email_narrative(
                    po["po_no"], new_eta_d, reason, orig, as_of)
            else:
                fn = {"formal": _email_formal, "semi": _email_semi}[style]
                subj, body, strength = fn(po["po_no"], new_eta_d, reason, orig)
            new_eta, ctype = new_eta_d.isoformat(), "delay"

        eid = f"GEN-{i+1:03d}"
        emails.append({
            "email_id": eid,
            "supplier_id": po["supplier_id"],
            "received_at": (datetime.combine(as_of, datetime.min.time())
                            - timedelta(hours=random.randint(0, 40))).strftime("%Y-%m-%d %H:%M"),
            "subject": subj,
            "body": body,
            "style": style,
            "tags": [style],
        })
        truth.append({
            "email_id": eid, "po_no": po["po_no"], "new_eta": new_eta,
            "commitment_strength": strength, "change_type": ctype,
            "reason_code": reason if ctype != "no_change" else "not_stated",
        })
    return emails, truth


def main() -> None:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = _load_config()["data_generation"]
    random.seed(cfg["seed"])
    as_of = date.fromisoformat(cfg["as_of_date"])

    DATA.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    for old in INBOX.glob("*.txt"):
        old.unlink()

    suppliers = build_suppliers()
    materials = build_materials(cfg["n_materials"])
    pos = build_pos(materials, suppliers, cfg["n_purchase_orders"], as_of)
    gen_emails, gen_truth = build_emails(pos, cfg["n_generated_emails"], as_of)

    # 手寫刁鑽案例併入
    all_emails = list(gen_emails)
    all_truth = list(gen_truth)
    for hc in HANDCRAFTED:
        all_emails.append({
            "email_id": hc["email_id"], "supplier_id": hc["supplier_id"],
            "received_at": hc["received_at"], "subject": hc["subject"],
            "body": hc["body"], "style": "handcrafted", "tags": hc["tags"],
        })
        for g in hc["ground_truth"]:
            all_truth.append({"email_id": hc["email_id"], **g})

    import pandas as pd
    pd.DataFrame(suppliers).to_csv(DATA / "suppliers.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(materials).to_csv(DATA / "materials.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(pos).to_csv(DATA / "po_master.csv", index=False, encoding="utf-8-sig")

    # 信件以純文字檔落地，模擬「從信箱匯出的一批郵件」
    for e in all_emails:
        p = INBOX / f"{e['email_id']}.txt"
        p.write_text(
            f"From: {e['supplier_id']}\n"
            f"Date: {e['received_at']}\n"
            f"Subject: {e['subject']}\n"
            f"\n{e['body']}\n",
            encoding="utf-8",
        )
    (INBOX / "_index.json").write_text(
        json.dumps(all_emails, ensure_ascii=False, indent=2), encoding="utf-8")
    (INBOX / "_ground_truth.json").write_text(
        json.dumps(all_truth, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] suppliers      : {len(suppliers)}")
    print(f"[OK] materials      : {len(materials)}")
    print(f"[OK] purchase orders: {len(pos)}")
    print(f"[OK] emails         : {len(all_emails)}  (手寫刁鑽案例 {len(HANDCRAFTED)} 封)")
    print(f"[OK] ground truth   : {len(all_truth)} 筆")
    print(f"[OK] 輸出目錄       : {DATA}")


if __name__ == "__main__":
    main()

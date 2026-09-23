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
    from .domain import (CATEGORY_SPEC, CATEGORY_SUPPLIER_TYPE, CATEGORY_WEIGHTS,
                         MaterialCategory, SupplierType)
    from .handcrafted_emails import HANDCRAFTED
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from domain import (CATEGORY_SPEC, CATEGORY_SUPPLIER_TYPE, CATEGORY_WEIGHTS,
                        MaterialCategory, SupplierType)
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
#   光阻與特殊氣體的供應集中在少數幾家、交期波動較大；
#   化學品供應商多、替代性高，準交率普遍較穩。
#   研磨液（CMP_SLURRY）只有 SUP-L01 一家，全公司一律單一來源。
SUPPLIER_SEED = [
    ("SUP-W01", "Wafer-Alpha",     SupplierType.WAFER_MAKER,      0.90),
    ("SUP-W02", "Wafer-Bravo",     SupplierType.WAFER_MAKER,      0.86),
    ("SUP-W03", "Wafer-Charlie",   SupplierType.WAFER_MAKER,      0.78),  # 2026-03-01 起表現變差
    ("SUP-R01", "Resist-Delta",    SupplierType.RESIST_MAKER,     0.84),
    ("SUP-R02", "Resist-Echo",     SupplierType.RESIST_MAKER,     0.72),  # 體質最差
    ("SUP-G01", "Gas-Foxtrot",     SupplierType.GAS_SUPPLIER,     0.75),  # 2026-04-01 起改善
    ("SUP-G02", "Gas-Golf",        SupplierType.GAS_SUPPLIER,     0.88),
    ("SUP-C01", "Chem-Hotel",      SupplierType.CHEMICAL_SUPPLIER, 0.92),
    ("SUP-C02", "Chem-India",      SupplierType.CHEMICAL_SUPPLIER, 0.85),
    ("SUP-T01", "Target-Juliet",   SupplierType.TARGET_MAKER,     0.80),
    ("SUP-T02", "Target-Kilo",     SupplierType.TARGET_MAKER,     0.83),
    ("SUP-M01", "Mask-Lima",       SupplierType.MASK_SHOP,        0.91),
    ("SUP-M02", "Mask-Mike",       SupplierType.MASK_SHOP,        0.85),
    ("SUP-L01", "Slurry-November", SupplierType.SLURRY_MAKER,     0.83),  # 研磨液唯一供應商
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
NODES = ["N7", "N12", "N16", "N22", "N28"]
PRODUCT_CODES = ["XR3390", "KL2210", "MT8195", "AB7710", "CD4420",
                 "EF9930", "GH1180", "IJ6650", "KL7720", "MN3310"]

# CATEGORY_SPEC／CATEGORY_WEIGHTS 定義在 domain.py（單一事實來源）：
# generate_history.py 產生歷史單的數量與大批量判斷也要用同一份表，
# 兩處各寫一份會走鐘——這正是「歷史單數量跟料別對不起來」這個 bug 的成因。


def _new_material_id(cat: str) -> str:
    if cat == "SILICON_WAFER":
        return f"SW-300-{random.choice('PE')}-{random.randint(1000, 9999)}"
    if cat == "PHOTORESIST":
        return f"PR-{random.choice(['ArF', 'KrF', 'EUV', 'iLine'])}-{random.randint(1000, 9999)}"
    if cat == "SPECIALTY_GAS":
        return f"GS-{random.choice(['NF3', 'WF6', 'SiH4', 'NH3', 'C4F6'])}-{random.randint(10, 99)}"
    if cat == "WET_CHEMICAL":
        return f"CH-{random.choice(['H2SO4', 'HF', 'H2O2', 'NH4OH', 'IPA'])}-{random.randint(10, 99)}"
    if cat == "TARGET":
        return f"TG-{random.choice(['Ti', 'Ta', 'Cu', 'Co', 'W'])}-{random.randint(10, 99)}"
    if cat == "MASK":
        return f"MSK-{random.choice(NODES)}-{random.choice(PRODUCT_CODES)}"
    return f"SL-{random.choice(['Cu', 'Ox', 'W', 'STI'])}-{random.randint(10, 99)}"


def build_materials(n: int) -> list[dict]:
    gr_days = _load_config()["receiving"]["gr_processing_days"]
    rows: list[dict] = []
    # 先放進手寫案例會用到的固定料號，確保 demo 情境可重現
    fixed = [
        ("SW-300-E-3390",  MaterialCategory.SILICON_WAFER, 110, True,  False, ""),
        ("SW-300-P-2210",  MaterialCategory.SILICON_WAFER, 90,  False, True,  "SW-300-P-2211"),
        ("SW-300-P-2211",  MaterialCategory.SILICON_WAFER, 90,  False, True,  "SW-300-P-2210"),
        ("MSK-N7-XR3390",  MaterialCategory.MASK,          21,  True,  False, ""),
        ("PR-ArF-1088",    MaterialCategory.PHOTORESIST,   75,  True,  False, ""),
        ("GS-NF3-01",      MaterialCategory.SPECIALTY_GAS, 45,  False, True,  ""),
        ("CH-HF-05",       MaterialCategory.WET_CHEMICAL,  20,  False, True,  ""),
        ("TG-Ti-77",       MaterialCategory.TARGET,        80,  False, True,  ""),
        ("TG-Ta-79",       MaterialCategory.TARGET,        85,  True,  False, ""),
        ("TG-Cu-90",       MaterialCategory.TARGET,        80,  False, True,  "TG-Cu-91"),
        ("TG-Cu-91",       MaterialCategory.TARGET,        80,  False, True,  "TG-Cu-90"),
    ]
    for mid, cat, lt, bottleneck, second_src, alt in fixed:
        rows.append({
            "material_id": mid,
            "category": cat.value,
            "std_lead_time_days": lt,
            "is_bottleneck": bottleneck,
            "has_qualified_second_source": second_src,
            "alt_material_id": alt,
            "criticality": "high" if bottleneck else ("medium" if lt >= 60 else "low"),
            "base_uom": CATEGORY_SPEC[cat.value]["uom"],
            "gr_processing_days": gr_days[cat.value],
        })

    while len(rows) < n:
        cat = random.choices(list(CATEGORY_WEIGHTS), weights=list(CATEGORY_WEIGHTS.values()))[0]
        mid = _new_material_id(cat)
        if any(r["material_id"] == mid for r in rows):
            continue
        lo, hi = CATEGORY_SPEC[cat]["lt"]
        lt = random.randint(lo, hi)
        # 領域假設：約 1/4 的料是瓶頸料。瓶頸料通常也就是沒有二源的那些
        #   —— 因為有二源的料，本來就不太會變成瓶頸。這個相關性是刻意做進去的。
        bottleneck = random.random() < 0.25
        # 領域假設：研磨液（CMP_SLURRY）全公司只有 SUP-L01 一家，
        #   不存在「已認證二源」這回事，強制設為 False。
        second_src = cat != "CMP_SLURRY" and (not bottleneck) and random.random() < 0.55
        rows.append({
            "material_id": mid,
            "category": cat,
            "std_lead_time_days": lt,
            "is_bottleneck": bottleneck,
            "has_qualified_second_source": second_src,
            "alt_material_id": "",
            "criticality": "high" if bottleneck else ("medium" if lt >= 60 else "low"),
            "base_uom": CATEGORY_SPEC[cat]["uom"],
            "gr_processing_days": gr_days[cat],
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
#   一旦已排入投片或機台排程、或業務已經對客戶報了交期，
#   上游延一天的代價會放大很多倍 —— 因為要動的不只這一張單。
FIXED_POS = [
    # (po_no, material_id, supplier_id, qty, committed, need_date,
    #  downstream_scheduled, reschedule_count, share_of_demand, split)
    #
    # split：固定的分批交貨，None 表示不分批。有值時是
    # (第二批交期, 第一批數量, 第二批數量)，兩批數量須相加等於 qty。
    # 只有明確給值的這一筆會分批 —— 其餘固定單依賴單一承諾日展示與測試，
    # 不能被隨機拆批邏輯誤觸。
    ("PO-2026-04417", "SW-300-E-3390", "SUP-W01", 3000, "2026-10-15", "2026-10-22", True,  2, 0.80, None),
    ("PO-2026-04452", "SW-300-P-2210", "SUP-W01", 2000, "2026-10-20", "2026-11-10", False, 0, 0.35, None),
    ("PO-2026-04390", "SW-300-P-2210", "SUP-W02", 2500, "2026-09-25", "2026-10-05", True,  1, 0.55, None),
    ("PO-2026-04391", "SW-300-P-2210", "SUP-W02", 2500, "2026-10-02", "2026-10-30", False, 0, 0.45, None),
    ("PO-2026-04205", "PR-ArF-1088",   "SUP-R02", 120,  "2026-09-10", "2026-09-18", True,  3, 0.95, None),
    ("PO-2026-04501", "GS-NF3-01",     "SUP-G02", 60,   "2026-09-30", "2026-10-25", False, 0, 0.30, None),
    ("PO-2026-04333", "CH-HF-05",      "SUP-C02", 80,   "2026-09-20", "2026-09-28", True,  1, 0.60, None),
    ("PO-2026-04466", "SW-300-P-2211", "SUP-W02", 1800, "2026-09-30", "2026-10-20", False, 0, 0.40, None),
    ("PO-2026-04120", "MSK-N7-XR3390", "SUP-M01", 1,    "2026-09-12", "2026-09-16", True,  0, 1.00, None),
    ("PO-2026-04277", "TG-Ti-77",      "SUP-T01", 20,   "2026-10-05", "2026-10-28", False, 0, 0.50, None),
    ("PO-2026-04278", "TG-Ti-77",      "SUP-T01", 20,   "2026-10-10", "2026-10-18", True,  2, 0.70, None),
    ("PO-2026-04279", "TG-Ta-79",      "SUP-T01", 30,   "2026-10-12", "2026-11-05", False, 3, 0.85, None),
    # 手寫案例 HC-010 的分批交貨：8 片照原日期出、12 片延到 11/15。
    ("PO-2026-04188", "TG-Cu-90",      "SUP-T02", 20,   "2026-09-30", "2026-10-08", True,  1, 0.75,
     ("2026-11-15", 8, 12)),
]


def _consistent_share(qty: int, target: float) -> float:
    """
    佔當期需求的比例，由「整數的當期需求量」反推，而不是直接抽一個比例。

    踩坑紀錄：光罩一次只買 1 片，直接抽出「佔 70%」會得到當期需求 1/0.7 ≈ 1.4 片，
    ERP 的請購單只能存整數 1 片，反推回來變成 100%，CSV 與模擬 ERP 的數字對不上。
    先決定整數需求量再算比例，兩邊就一定一致。
    """
    period_qty = max(qty, int(round(qty / target)))
    return round(qty / period_qty, 2)


# 領域假設：約 20% 的單分兩批交貨（先出一部分、其餘延後）——
#   這是物料企劃最常見卻最少被工具處理的回覆型態之一。
#   光罩一次只買 1 片，不會分批（qty=1 沒有「先出 0.3 片」這種事）。
#
# 踩坑紀錄：一開始直接用全域 random 抽「要不要分批」，多插進去的抽樣
# 會往後撬動每一張後續生成單的料號／供應商抽樣序列 —— 結果某供應商
# 因此再也沒被抽中當任何料號的主要供應商，來源清單裡整個消失，
# 連帶歷史單怎麼抽都抽不到它（見 tests/test_generate_history.py 的漂移測試）。
# 分批決策改用獨立的 RNG，不去動全域 random 的抽樣序列。
def _random_split_schedule(qty: int, committed: date,
                           rng: random.Random) -> list[tuple[int, str, int]]:
    """
    隨機決定一張單是否分兩批交貨，回傳排程行清單 [(sched_line, committed_date, qty), ...]。

    第二批比第一批晚 7～30 天；數量拆法為 30/70 或 50/50，取整數後
    仍確保兩批相加等於總量、且每批至少 1 個單位（避免出現「這批 0 件」）。
    """
    if qty > 1 and rng.random() < 0.20:
        frac = rng.choice([0.3, 0.5])
        qty1 = max(1, min(qty - 1, round(qty * frac)))
        qty2 = qty - qty1
        date2 = committed + timedelta(days=rng.randint(7, 30))
        return [(1, committed.isoformat(), qty1), (2, date2.isoformat(), qty2)]
    return [(1, committed.isoformat(), qty)]


def _explode_schedule(rows: list[dict]) -> list[dict]:
    """
    把「一張單一列」的內部資料，展開成「每筆交貨排程行一列」寫進 CSV。

    領域假設／教訓：分批交貨在 ERP 裡本來就是多筆排程行；CSV 若維持一張單一列，
    等於在資料層就把「先到的那批」丟掉了，下游不管怎麼寫都救不回來
    （這正是 sqlite_source.py 原本 latest_sched CTE 的簡化，見 docs/設計決策.md 決策 19）。

    share_of_period_demand 改成「這一批」的佔比（分子是 sched_qty，不是項次總量），
    對齊 SqliteSource 用 sched_qty／period_demand_qty 算出來的版本——
    否則 CSV 與 SQLite 兩種來源在分批交貨的單上會對不出一致的佔比。
    """
    exploded: list[dict] = []
    for r in rows:
        qty = int(r["qty"])
        total_share = float(r["share_of_period_demand"]) or 1.0
        period_qty = max(qty, int(round(qty / total_share)))
        for sched_line, committed_date, sched_qty in r["schedule"]:
            row = {k: v for k, v in r.items() if k != "schedule"}
            row["committed_date"] = committed_date
            row["sched_line"] = sched_line
            row["sched_qty"] = sched_qty
            row["share_of_period_demand"] = round(sched_qty / period_qty, 2)
            exploded.append(row)
    return exploded


def build_pos(materials: list[dict], suppliers: list[dict], n: int, as_of: date) -> list[dict]:
    cfg = _load_config()
    gr_days = cfg["receiving"]["gr_processing_days"]
    # 獨立於全域 random 的分批決策亂數來源，理由見 _random_split_schedule 上方註解。
    # 種子固定（用資料產生器同一組 seed 派生），確保跑起來可重現。
    split_rng = random.Random(cfg["data_generation"]["seed"] ^ 0x53504C54)  # "SPLT"
    by_id = {m["material_id"]: m for m in materials}
    rows: list[dict] = []

    for (po, mid, sup, qty, committed, need, sched, resched, share, split) in FIXED_POS:
        if mid not in by_id:  # 固定料號若不在主檔則補上，確保 demo 一定跑得起來
            by_id[mid] = {
                "material_id": mid, "category": MaterialCategory.SILICON_WAFER.value,
                "std_lead_time_days": 90, "is_bottleneck": True,
                "has_qualified_second_source": False, "alt_material_id": "",
                "criticality": "high", "base_uom": "PCS",
                "gr_processing_days": gr_days[MaterialCategory.SILICON_WAFER.value],
            }
            materials.append(by_id[mid])
        # 領域假設：下單日 = 承諾日往前推「標準前置期＋一點下單前置作業
        #   （0-14 天）」，不是跟 as_of 綁死的固定區間——固定 30-120 天
        #   會讓矽晶圓（前置期常常就超過 90 天）看起來像壓線下單。
        lt = int(by_id[mid]["std_lead_time_days"])
        if split:
            date2, qty1, qty2 = split
            assert qty1 + qty2 == qty, f"{po} 分批數量沒有加總等於項次總量"
            schedule = [(1, committed, qty1), (2, date2, qty2)]
        else:
            schedule = [(1, committed, qty)]
        rows.append({
            "po_no": po, "material_id": mid, "supplier_id": sup, "qty": qty,
            "committed_date": committed, "need_date": need,
            "downstream_scheduled": sched, "reschedule_count": resched,
            "share_of_period_demand": share,
            "po_created_date": (date.fromisoformat(committed)
                               - timedelta(days=lt + random.randint(0, 14))).isoformat(),
            "schedule": schedule,
        })

    seq = 4600
    while len(rows) < n:
        seq += random.randint(1, 4)
        m = random.choice(materials)
        # 領域假設：供應商類型要和料號類別對得起來 —— 光阻不會去跟氣體廠買。
        wanted = CATEGORY_SUPPLIER_TYPE[m["category"]]
        cands = [s for s in suppliers if s["supplier_type"] == wanted]
        sup = random.choice(cands)["supplier_id"]

        committed = as_of + timedelta(days=random.randint(-10, 100))
        # 領域假設：MRP 排需求日時已經扣掉收貨處理時間，所以正常情況下
        #   承諾日至少要比需求日早「收貨處理天數」，不能比它還晚——
        #   否則等於 MRP 排的需求日打從一開始就沒扣到這段處理時間。
        #   瓶頸料的緩衝通常更薄，因為排程壓得緊，沒有多餘空間；
        #   這個相關性刻意做進去。
        gr = int(gr_days.get(m["category"], 0))
        buffer_days = (random.randint(gr, max(gr, 12)) if m["is_bottleneck"]
                      else random.randint(max(3, gr), 35))
        lt = int(m["std_lead_time_days"])
        qty = random.choice(CATEGORY_SPEC[m["category"]]["qty"])
        rows.append({
            "po_no": f"PO-2026-{seq:05d}",
            "material_id": m["material_id"],
            "supplier_id": sup,
            "qty": qty,
            "committed_date": committed.isoformat(),
            "need_date": (committed + timedelta(days=buffer_days)).isoformat(),
            # 領域假設：越接近交期，下游越可能已經排定。
            "downstream_scheduled": (committed - as_of).days < 30 and random.random() < 0.6,
            # 領域假設：改期次數呈長尾 —— 多數 PO 沒改過，少數改到爛。
            #   而且改過的更容易再改（這正是「累犯」規則存在的理由）。
            "reschedule_count": random.choices([0, 1, 2, 3, 4], weights=[0.55, 0.22, 0.13, 0.07, 0.03])[0],
            "share_of_period_demand": _consistent_share(qty, random.uniform(0.15, 1.0)),
            "po_created_date": (committed - timedelta(days=lt + random.randint(0, 14))).isoformat(),
            "schedule": _random_split_schedule(qty, committed, split_rng),
        })
    return rows


# ---------------------------------------------------------------------------
# 信件產生
# ---------------------------------------------------------------------------
# 各種書寫風格。刻意做出「規則層抓得到」與「規則層抓不到」兩群，
# 對照實驗才有意義 —— 若全部都是格式化郵件，會得出「不需要 LLM」的假結論。
REASON_TEXT_EN = {
    "capacity": "capacity constraint at our plant",
    "yield": "a quality excursion (lot out of spec)",
    "upstream_shortage": "upstream raw material shortage",
    "logistics": "customs clearance delay",
    "customer_priority": "allocation adjustment",
    "internal_reschedule": "internal reschedule",
}
REASON_TEXT_ZH = {
    "capacity": "產線產能吃緊",
    "yield": "品質異常（批號規格不符）",
    "upstream_shortage": "上游原料短缺",
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
    # po_master.csv 是每筆交貨排程行一列（分批交貨時同一張單有多列），
    # 不是每張單一列 —— build_emails() 用的是還沒展開的 pos（見上方呼叫），
    # 因為信件內容談的是「這張單」，不是某一筆排程行。
    pd.DataFrame(_explode_schedule(pos)).to_csv(
        DATA / "po_master.csv", index=False, encoding="utf-8-sig")

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

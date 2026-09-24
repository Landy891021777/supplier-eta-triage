# -*- coding: utf-8 -*-
"""
產生「歷史單據與實際結果」，寫入模擬 ERP 資料庫。

先前的模擬 ERP 只有「未結採購單」。`goods_receipt`（收貨紀錄）代表
「後來到底怎麼了」，沒有它就無法從歷史推導供應商準交率與到料估計。
這支程式補上過去 12 個月的歷史單據：請購 → 採購 → 交貨排程 →
改期紀錄 → 實際收貨。

===========================  必須先說的話  ===========================
**這些歷史結果是我用一組因果規則產生的，不是真實資料。**

它的用途是讓「歷史落差 → 保守到料日 → 回測」這條流程有資料可跑。
回測（src/backtest.py）驗證的是方法有沒有偷看未來、估計有沒有校準，
**不能證明真實供應商會照這種分布行動。**

為了讓時間切分回測有意義，部分供應商的表現會隨時間變好或變差
（見 SUPPLIER_DRIFT）。若整年都是同一個固定分布，涵蓋率必然接近設定值，
驗證就沒有意義。
=====================================================================
"""
from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

try:  # 允許以 `python src/generate_history.py` 或 `python -m src.generate_history` 執行
    from .domain import CATEGORY_SPEC
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from domain import CATEGORY_SPEC

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp_sim.db"

HISTORY_MONTHS = 12
# 歷史單總數的依據：每家供應商「改期過的已結案單」至少 20 張，
# 保守到料日才有足夠樣本、不必退回全部單估計（估計的最低樣本數也是 20）。
# 14 家供應商下，900 張時有 4 家不足、1200 張仍有 1 家不足，1500 張才全部達標。
# 領域假設：一座晶圓廠一年對 14 家生產用料供應商下 1500 張採購單並不誇張。
N_HISTORY = 1500
SEED = 20260101


def _load_cfg() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _is_quarter_end(d: date) -> bool:
    """
    領域假設：季末最後兩週的交付達成率明顯較差。

    來源：廠內的產能結帳行為 —— 季末衝出貨、資源被優先客戶佔用，
    排在那段時間的單最容易被往後挪。這是我在供應商端實際觀察到的模式。
    """
    q_end_month = ((d.month - 1) // 3 + 1) * 3
    q_end = date(d.year + (q_end_month == 12 and d.month == 12 and 0 or 0),
                 q_end_month, 1)
    nxt = date(q_end.year + (q_end.month == 12), (q_end.month % 12) + 1, 1)
    q_end = nxt - timedelta(days=1)
    return 0 <= (q_end - d).days <= 14


# ---------------------------------------------------------------------------
# 因果模型：世界實際上是怎麼運作的
# ---------------------------------------------------------------------------
# 世界的驅動力 = 供應商體質、料件類別、瓶頸、累犯、季末、批量、隨時間漂移。
# 工具只用「供應商 × 是否改期過」估計落差，刻意沒有照抄這些因子 ——
# 估計方法沒看到的因子，正是回測要檢驗它會不會因此失準的地方。
#
# 領域假設：光阻與特殊氣體的供應集中在少數幾家、交期波動最大；
#   化學品供應商多、替代性高，波動最小；光罩多為客製、前置期短，
#   一旦異常較難用「提早下單」吸收，但波動本身不算大。
CATEGORY_RISK = {
    "PHOTORESIST": 0.10, "SPECIALTY_GAS": 0.08, "TARGET": 0.06,
    "SILICON_WAFER": 0.05, "CMP_SLURRY": 0.04, "WET_CHEMICAL": 0.03, "MASK": 0.02,
}

# 領域假設：供應商的交付表現不是固定不變的。
# 現實中會因為產能重分配、換廠、良率改善而變好或變差。
# from：從這天起（以承諾日計）表現改變；p_late：延遲機率的加減量；
# delay_mult：延遲時的天數倍率。
SUPPLIER_DRIFT = {
    "SUP-W03": {"from": date(2026, 3, 1), "p_late": +0.25, "delay_mult": 1.4},  # 變差
    "SUP-G01": {"from": date(2026, 4, 1), "p_late": -0.22, "delay_mult": 0.8},  # 改善
}


def _simulate_outcome(rng: random.Random, *, vendor_id: str, otd_rate: float,
                      category: str, is_bottleneck: bool, reschedule_count: int,
                      committed: date, qty: int) -> int:
    """
    回傳「實際到料日 − 承諾日」的天數（負值代表提前到料）。

    兩段式：先決定會不會延，再決定延多久。
    這比直接抽一個延遲天數更貼近實務 —— 大多數單其實是準時的，
    問題集中在少數幾張。
    """
    p_late = (1.0 - otd_rate)
    p_late += CATEGORY_RISK.get(category, 0.05)
    if is_bottleneck:
        p_late += 0.08                      # 瓶頸料排程壓得緊，沒有緩衝
    p_late += min(0.20, 0.06 * reschedule_count)   # 領域假設：改過期的更容易再改
    if _is_quarter_end(committed):
        p_late += 0.10
    # 領域假設：該料別最大的常見下單量較難一次做完；光罩一次一片，
    #   不適用（CATEGORY_SPEC["MASK"]["qty"] 只有一個選項，len(opts) > 1
    #   為 False，不會被判成大批量）。改用「這個料別自己的最大選項」而
    #   不是寫死的 5000，因為 5000 對矽晶圓是常態、對光阻卻是不可能出現
    #   的數字——寫死的門檻等於讓每個料別的「大批量」機率各自不同、
    #   卻沒人刻意這樣設計過。
    opts = CATEGORY_SPEC.get(category, {}).get("qty", [])
    large = len(opts) > 1 and qty == max(opts)
    if large:
        p_late += 0.04
    drift = SUPPLIER_DRIFT.get(vendor_id)
    drift_on = bool(drift and committed >= drift["from"])
    if drift_on:
        p_late += drift["p_late"]
    p_late = max(0.02, min(0.92, p_late))

    if rng.random() >= p_late:
        # 準時或稍微提前。提前交貨在實務上也會發生，且會造成倉容與付款問題。
        return -rng.randint(0, 3)

    # 延遲幅度：長尾分布 —— 多數延一兩週，少數延到爆炸。
    base = rng.lognormvariate(2.1, 0.75)     # 中位數約 8 天
    if is_bottleneck:
        base *= 1.35
    base *= (1.0 + 0.12 * reschedule_count)
    # 領域假設：光阻供應商少、批次生產週期長，一旦延遲，補回來的時間也更長。
    if category == "PHOTORESIST":
        base *= 1.3
    if drift_on:
        base *= drift["delay_mult"]
    return max(1, int(round(base)))


def _clear_previous_history(con: sqlite3.Connection) -> None:
    """
    先清掉上一次產生的歷史單據，再重新寫入。

    這段是踩坑後補的：原本直接 INSERT，重跑一次就把變更文件寫入第二遍，
    `po_change_log` 從 904 筆變成 1599 筆，改期次數憑空翻倍
    （有些單變成「改期 8 次」），供應商統計因此每跑一次就不一樣。

    這種 bug 特別惡劣的地方在於：**它不會報錯，資料照樣跑得出來，
    只是悄悄地錯。** 而且它直接違反專案標榜的「可重現」。

    產生器必須是冪等的 —— 跑一次跟跑十次結果要一樣。
    歷史單以請購單號前綴 `PR-H` 辨識，那是本程式的專屬命名空間。
    """
    hist_pos = [r[0] for r in con.execute(
        "SELECT DISTINCT po_no FROM po_item WHERE pr_no LIKE 'PR-H%'")]
    if not hist_pos:
        return
    marks = ",".join("?" * len(hist_pos))
    for table in ("goods_receipt", "po_change_log", "po_schedule",
                  "po_item", "po_header"):
        con.execute(f"DELETE FROM {table} WHERE po_no IN ({marks})", hist_pos)
    con.execute("DELETE FROM purchase_req WHERE pr_no LIKE 'PR-H%'")
    con.commit()


def build_history(verbose: bool = True, db_path: Path | str | None = None) -> dict:
    path = Path(db_path or DB_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 {path}，請先執行： py src/build_erp_db.py")

    cfg = _load_cfg()
    as_of = date.fromisoformat(cfg["data_generation"]["as_of_date"])
    rng = random.Random(SEED)

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    _clear_previous_history(con)

    vendors = {r["vendor_id"]: dict(r)
               for r in con.execute("SELECT * FROM vendor_master")}
    materials = {r["material_id"]: dict(r)
                 for r in con.execute("SELECT * FROM material_master")}
    # 只從來源清單挑合格供應商 —— 歷史單也必須符合來源清單的約束
    sources: dict[str, list[str]] = {}
    for r in con.execute("SELECT material_id, vendor_id FROM source_list "
                         "WHERE is_qualified = 1"):
        sources.setdefault(r["material_id"], []).append(r["vendor_id"])

    usable = [m for m in materials if sources.get(m)]
    if not usable:
        raise RuntimeError("來源清單是空的，無法產生歷史單據")

    start = as_of - timedelta(days=30 * HISTORY_MONTHS)
    pr_rows, hdr, item, sched, chg, gr = [], [], [], [], [], []
    seq = 1000

    for _ in range(N_HISTORY):
        seq += rng.randint(1, 3)
        mid = rng.choice(usable)
        m = materials[mid]
        vid = rng.choice(sources[mid])
        v = vendors[vid]

        # 承諾日落在歷史區間內，且必須早於今天（否則不算歷史單）
        committed = start + timedelta(days=rng.randint(0, 30 * HISTORY_MONTHS - 20))
        if committed >= as_of:
            continue

        # 數量依料別的常見下單量抽，跟 generate_data.py 用同一份表
        # （domain.CATEGORY_SPEC）——先前各寫各的，光罩、光阻這種料
        # 從沒出現過的 5000／8000 這種矽晶圓等級的數量，在 ERP 分頁
        # 可以直接看到，一眼就穿幫；fallback 只在料別不在表裡時才用。
        qty = rng.choice(CATEGORY_SPEC.get(m["category"], {}).get(
            "qty", [500, 1000, 1500, 2000, 3000, 5000, 8000]))
        # 領域假設：瓶頸料的緩衝天數更薄（排程壓得緊，沒有多餘空間）
        buffer_days = (rng.randint(0, 12) if m["is_bottleneck"]
                       else rng.randint(3, 35))
        need = committed + timedelta(days=buffer_days)
        n_resched = rng.choices([0, 1, 2, 3, 4],
                                weights=[0.55, 0.22, 0.13, 0.07, 0.03])[0]

        # ---- 先算結果，再決定要不要落單 ----
        # 這支程式的職責是產生「已結案」的歷史單。若模擬出來的到料日還沒到，
        # 這張單其實是在途單，不屬於歷史 —— 硬寫進去會多出一批沒有結果的
        # 採購單，混進今日行動清單，讓「在途單」的集合莫名其妙變多。
        delta = _simulate_outcome(
            rng, vendor_id=vid, otd_rate=float(v["otd_rate"] or 0.85), category=m["category"],
            is_bottleneck=bool(m["is_bottleneck"]), reschedule_count=n_resched,
            committed=committed, qty=qty)
        receipt = committed + timedelta(days=delta)
        if receipt >= as_of:
            continue

        po_no = f"PO-{committed.year}-{seq:05d}"
        pr_no = f"PR-H{seq:06d}"
        share = round(rng.uniform(0.15, 1.0), 2)
        period_qty = max(qty, int(round(qty / share)))

        pr_rows.append((pr_no, mid, qty, need.isoformat(), period_qty,
                        int((committed - as_of).days < 30 and rng.random() < 0.6)))
        # 領域假設：下單日 = 承諾日往前推「標準前置期＋一點下單前置作業
        #   （0-14 天）」，不是跟承諾日無關的固定 30-150 天——矽晶圓的
        #   標準前置期常常就超過 90 天，固定區間會讓下單日看起來比
        #   合約前置期還晚，story 對不起來。
        lt = int(m["std_lead_time_days"])
        hdr.append((po_no, vid,
                    (committed - timedelta(days=lt + rng.randint(0, 14))).isoformat()))
        item.append((po_no, 10, mid, qty, pr_no))
        sched.append((po_no, 10, 1, committed.isoformat(), qty))

        for k in range(n_resched):
            old = committed - timedelta(days=7 * (n_resched - k))
            new = committed - timedelta(days=7 * (n_resched - k - 1))
            chg.append((po_no, 10, "committed_date", old.isoformat(),
                        new.isoformat(),
                        (old - timedelta(days=3)).isoformat(), "VENDOR_EDI"))

        gr.append((f"GR-{seq:06d}", po_no, 10, qty, receipt.isoformat()))

    con.executemany("INSERT OR IGNORE INTO purchase_req VALUES (?,?,?,?,?,?)", pr_rows)
    con.executemany("INSERT OR IGNORE INTO po_header VALUES (?,?,?)", hdr)
    con.executemany("INSERT OR IGNORE INTO po_item VALUES (?,?,?,?,?)", item)
    con.executemany("INSERT OR IGNORE INTO po_schedule VALUES (?,?,?,?,?)", sched)
    con.executemany(
        "INSERT INTO po_change_log (po_no,item_no,field_name,old_value,new_value,"
        "changed_at,changed_by) VALUES (?,?,?,?,?,?,?)", chg)
    con.executemany("INSERT OR IGNORE INTO goods_receipt VALUES (?,?,?,?,?)", gr)
    con.commit()

    stats = {
        "歷史採購單": len(gr),
        "收貨紀錄": con.execute("SELECT COUNT(*) FROM goods_receipt").fetchone()[0],
        "變更文件（含歷史）": con.execute("SELECT COUNT(*) FROM po_change_log").fetchone()[0],
    }
    # M-9：料到廠不代表能投產，「真正造成缺料」的定義要跟 backtest.py 的
    # actual_short 一致——加上該料別的收貨處理天數才算，不是收貨當天。
    # 這裡改成在 Python 端逐列算（不是純 SQL），因為 gr 天數依料別而不同。
    gr_cfg = cfg.get("receiving", {}).get("gr_processing_days", {})
    late_rows = con.execute("""
        SELECT g.receipt_date, s.committed_date, r.need_date, m.category
        FROM goods_receipt g
        JOIN po_item i          ON i.po_no = g.po_no AND i.item_no = g.item_no
        JOIN po_schedule s      ON s.po_no = g.po_no AND s.item_no = g.item_no
        JOIN purchase_req r     ON r.pr_no = i.pr_no
        JOIN material_master m  ON m.material_id = i.material_id
    """).fetchall()
    con.close()

    n = len(late_rows)
    late_commit = sum(1 for row in late_rows if row["receipt_date"] > row["committed_date"])
    late_need = sum(1 for row in late_rows if row["receipt_date"] > row["need_date"])
    late_need_gr = sum(
        1 for row in late_rows
        if (date.fromisoformat(row["receipt_date"])
            + timedelta(days=int(gr_cfg.get(row["category"], 0)))).isoformat() > row["need_date"])

    if verbose:
        print(f"[OK] 歷史單據已寫入 {path.name}")
        for k, val in stats.items():
            print(f"       {k:<20} {val:>6}")
        if n:
            print(f"       其中晚於承諾日          {late_commit:>6}  "
                  f"({late_commit / n * 100:.1f}%)")
            print(f"       到貨日晚於需求日        {late_need:>6}  "
                  f"({late_need / n * 100:.1f}%)")
            print(f"       可投產日晚於需求日      {late_need_gr:>6}  "
                  f"({late_need_gr / n * 100:.1f}%)  <- 加上收貨處理天數，真正造成缺料的")
        print()
        print("       提醒：這些結果由因果模型產生，非真實資料。")
        print("       回測只能證明方法可行，不能證明真實供應商的行為。")
    return stats


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    build_history()

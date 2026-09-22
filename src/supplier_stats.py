# -*- coding: utf-8 -*-
"""
供應商歷史表現：把 ERP 收貨紀錄變成生管當下用得到的決策依據。

===========================  為什麼需要這一支  ===========================
歷史單據現在餵給兩件事：行動清單上的保守到料日估計，
以及 `src/backtest.py` 的時間切分回測。**沒有這一支，生管在每天的
行動清單上，看不到任何歷史資訊。**

歷史資料真正的價值，是回答生管看到一封延遲通知時心裡的那個問題：

    「他說 10/29。這家供應商說的話，到底能信幾分？實際大概會是哪天？」

這支程式從 `goods_receipt` 與 `po_schedule` 算出三件事：

  1. 這家供應商的實際準交率與延遲天數分布
  2. **改期過的單，最終是不是更容易再延** —— 驗證「累犯」這條規則
  3. 依歷史分布推出的「保守到料日」，供排程參考

===========================  這不是預測模型  ===========================
以上全部是**歷史統計**（比例、中位數、百分位），不是機器學習預測。

差別很重要：統計說的是「過去這家供應商有 26% 的單延遲，延遲時中位數 9 天」，
它不宣稱知道「這一張單會怎樣」。生管看得懂、也可以自己驗證，
而且不需要任何模型維護成本。

保守到料日採 P80（歷史上 80% 的單在這天以前到），刻意不用平均值 ——
平均值會被少數超長延遲拉高，也不符合排程要的是「幾成把握」的語言。
=====================================================================
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp_sim.db"

# 已結案單的實際表現。delay_days > 0 代表比承諾日晚到。
# notice_date：最後一次「承諾日被改」的變更日，也就是企劃收到改期通知的時點。
#   回測用它決定「當時已經知道哪些歷史」。
HISTORY_SQL = """
SELECT
    i.po_no                                                       AS po_no,
    h.vendor_id                                                   AS supplier_id,
    m.category                                                    AS category,
    s.committed_date                                              AS committed_date,
    g.receipt_date                                                AS receipt_date,
    r.need_date                                                   AS need_date,
    v.otd_rate                                                    AS vendor_otd,
    CAST(julianday(g.receipt_date) - julianday(s.committed_date)
         AS INTEGER)                                              AS delay_days,
    (SELECT COUNT(*) FROM po_change_log c
      WHERE c.po_no = i.po_no AND c.item_no = i.item_no
        AND c.field_name = 'committed_date')                      AS reschedule_count,
    (SELECT MAX(c.changed_at) FROM po_change_log c
      WHERE c.po_no = i.po_no AND c.item_no = i.item_no
        AND c.field_name = 'committed_date')                      AS notice_date
FROM goods_receipt g
JOIN po_item      i ON i.po_no = g.po_no AND i.item_no = g.item_no
JOIN po_header    h ON h.po_no = i.po_no
JOIN po_schedule  s ON s.po_no = i.po_no AND s.item_no = i.item_no
JOIN material_master m ON m.material_id = i.material_id
LEFT JOIN purchase_req   r ON r.pr_no = i.pr_no
LEFT JOIN vendor_master  v ON v.vendor_id = h.vendor_id
"""


def load_outcomes(db_path: Path | str | None = None) -> pd.DataFrame:
    path = Path(db_path or DB_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 {path}，請先執行： py src/build_erp_db.py 與 py src/generate_history.py")
    with sqlite3.connect(path) as con:
        df = pd.read_sql_query(HISTORY_SQL, con)
    if df.empty:
        raise RuntimeError(
            "沒有收貨紀錄，請先執行： py src/generate_history.py")
    return df


def supplier_performance(df: pd.DataFrame | None = None,
                         min_samples: int = 20) -> pd.DataFrame:
    """
    每家供應商的歷史表現。

    `min_samples` 存在的理由：樣本太少的統計會誤導人。
    只有三張單的供應商算出「準交率 33%」，那個數字不該拿去做決策，
    也不該顯示給生管看 —— 工具寧可說「樣本不足」，也不要給一個
    看起來很精確的假訊號。
    """
    df = load_outcomes() if df is None else df
    g = df.groupby("supplier_id")["delay_days"]
    out = pd.DataFrame({
        "樣本數": g.size(),
        "準交率": (df.assign(ok=df["delay_days"] <= 0)
                   .groupby("supplier_id")["ok"].mean().round(3)),
        "延遲時中位數(天)": (df[df["delay_days"] > 0]
                            .groupby("supplier_id")["delay_days"].median()),
        "P80 延遲(天)": g.quantile(0.80, interpolation="higher").round(0),
        "最長延遲(天)": g.max(),
    })
    # 領域假設：這兩欄與 estimate_delay() 用同一個母體（改期過的單），
    # 讓畫面上的數字跟保守到料日的估計基礎一致，不會兩邊對不上。
    # 全部欄位的 P80 都採 interpolation="higher"：取實際出現過的天數，
    # 不做內插，畫面上每一個 P80 都是真的發生過的落差、彼此可比較。
    resched = df[df["reschedule_count"] >= 1].groupby("supplier_id")["delay_days"]
    out["改期單樣本數"] = resched.size().reindex(out.index).fillna(0).astype(int)
    out["改期單 P80 延遲(天)"] = (
        resched.quantile(0.80, interpolation="higher").reindex(out.index))
    out["樣本是否足夠"] = out["樣本數"] >= min_samples
    return out.reset_index()


def estimate_delay(outcomes: pd.DataFrame, supplier_id: str, *,
                   percentile: float, min_samples: int = 20) -> dict:
    """
    這家供應商「說定日期後」實際還會晚幾天（歷史百分位）。

    母體只取**曾改期過的單**：企劃收到的是已經跳票、剛給新日期的通知，
    拿「全部單」（含從沒改期、準時到的）去估，會系統性低估這種單的風險。
    改期單樣本不足 min_samples 時退回全部單，並在 basis 標明；
    全部單也不足就回報樣本不足，不給一個看起來很精確的假數字。

    刻意不再往下切（例如再依料別）：898 張歷史單分給 12 家供應商，
    每家改期單只有幾十筆，再切每格只剩個位數。

    這是歷史統計，不是預測模型。percentile 用 "higher"：取實際出現過的
    天數，不做內插，說出來的「N 天」一定是真的發生過的落差。
    """
    mine = outcomes[outcomes["supplier_id"] == supplier_id]
    # 領域假設：收貨紀錄不該有空日期；這行是防禦，避免單筆髒資料讓整個估計崩潰。
    mine = mine[pd.to_numeric(mine["delay_days"], errors="coerce").notna()]
    rescheduled = mine[mine["reschedule_count"] >= 1]
    if len(rescheduled) >= min_samples:
        pool, basis = rescheduled, "改期過的單"
    elif len(mine) >= min_samples:
        pool, basis = mine, "全部單（改期單樣本不足）"
    else:
        return {"available": False, "n": int(len(mine)),
                "reason": f"歷史樣本不足（{len(mine)} 筆），不提供保守估計"}
    values = pd.to_numeric(pool["delay_days"], errors="coerce").to_numpy(dtype=float)
    days = int(np.quantile(values, percentile, method="higher"))
    return {"available": True, "delay_days": max(0, days),
            "percentile": percentile, "basis": basis, "n": int(len(pool))}


def reschedule_reliability(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    驗證「累犯」這條規則：改期過的單，最終是不是真的更容易再延？

    這張表的價值不只是資訊，更是**規則的自我驗證**。
    如果資料顯示改期次數與最終延遲無關，那規則 7 就該被拿掉 ——
    工具必須容許自己的規則被自己的資料推翻。
    """
    df = load_outcomes() if df is None else df
    bucket = df["reschedule_count"].clip(upper=3)
    labels = {0: "未改期", 1: "改期 1 次", 2: "改期 2 次", 3: "改期 3 次以上"}
    tmp = df.assign(改期情形=bucket.map(labels),
                    late=df["delay_days"] > 0)
    out = tmp.groupby("改期情形").agg(
        樣本數=("late", "size"),
        最終仍延遲比例=("late", "mean"),
        延遲時中位數=("delay_days", lambda s: s[s > 0].median()),
    ).round(3)
    order = [labels[k] for k in sorted(labels) if labels[k] in out.index]
    return out.loc[order].reset_index()


def conservative_eta(supplier_id: str, promised: str | date,
                     outcomes: pd.DataFrame, *, percentile: float = 0.80,
                     min_samples: int = 20) -> dict:
    """
    把「供應商說的日期」翻譯成「保守到料日」。

    回傳的是給排程參考的第二個日期，不是預測：
    意思是「歷史上這類單有八成在這天以前到」。
    刻意不取平均：平均會被少數超長延遲拉高，
    而且排程要的是「幾成把握」的語言，不是期望值。
    """
    try:
        promised_d = (promised if isinstance(promised, date)
                      else date.fromisoformat(str(promised)[:10]))
    except (ValueError, TypeError):
        return {"available": False, "reason": "承諾日無法解析"}

    est = estimate_delay(outcomes, supplier_id, percentile=percentile,
                         min_samples=min_samples)
    if not est["available"]:
        return est
    return {
        **est,
        "promised": promised_d.isoformat(),
        "conservative": (promised_d + timedelta(days=est["delay_days"])).isoformat(),
        "note": (f"歷史{est['basis']}共 {est['n']} 筆，"
                 f"{int(round(percentile * 100))}% 在說定日期後 {est['delay_days']} 天內到"),
    }


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    data = load_outcomes()
    print(f"歷史已結案單：{len(data)} 筆\n")
    print("=== 各供應商歷史表現 ===")
    print(supplier_performance(data).to_string(index=False))
    print("\n=== 改期次數 vs 最終是否延遲（驗證『累犯』規則）===")
    print(reschedule_reliability(data).to_string(index=False))
    print("\n=== 保守到料日示例 ===")
    for sid, promised in [("SUP-F03", "2026-10-29"), ("SUP-S01", "2026-10-14")]:
        r = conservative_eta(sid, promised, data)
        if r["available"]:
            print(f"  {sid}  說定 {r['promised']} -> 保守 {r['conservative']}  （{r['note']}）")
        else:
            print(f"  {sid}  {r['reason']}")

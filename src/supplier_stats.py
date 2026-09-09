# -*- coding: utf-8 -*-
"""
供應商歷史表現：把 ERP 收貨紀錄變成生管當下用得到的決策依據。

===========================  為什麼需要這一支  ===========================
歷史單據原本只餵給了權重校準（`calibrate.py`），那是分析用的，
一年看一次。**生管在每天的行動清單上，看不到任何歷史資訊。**

但歷史資料真正的價值，是回答生管看到一封延遲通知時心裡的那個問題：

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

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "erp_sim.db"

# 已結案單的實際表現。delay_days > 0 代表比承諾日晚到。
HISTORY_SQL = """
SELECT
    h.vendor_id                                                   AS supplier_id,
    m.category                                                    AS category,
    CAST(julianday(g.receipt_date) - julianday(s.committed_date)
         AS INTEGER)                                              AS delay_days,
    (SELECT COUNT(*) FROM po_change_log c
      WHERE c.po_no = i.po_no AND c.item_no = i.item_no
        AND c.field_name = 'committed_date')                      AS reschedule_count
FROM goods_receipt g
JOIN po_item      i ON i.po_no = g.po_no AND i.item_no = g.item_no
JOIN po_header    h ON h.po_no = i.po_no
JOIN po_schedule  s ON s.po_no = i.po_no AND s.item_no = i.item_no
JOIN material_master m ON m.material_id = i.material_id
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
        "P80 延遲(天)": g.quantile(0.80).round(0),
        "最長延遲(天)": g.max(),
    })
    out["樣本是否足夠"] = out["樣本數"] >= min_samples
    return out.reset_index()


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
                     perf: pd.DataFrame | None = None) -> dict:
    """
    依歷史分布，把「供應商承諾日」翻譯成「保守到料日」。

    回傳的是給生管排程時參考的第二個日期，**不是預測**：
    意思是「歷史上這家供應商有八成的單在這天以前到」。

    刻意不取平均：平均會被少數超長延遲拉高，而且排程要的是
    「幾成把握」的語言，不是期望值。
    """
    perf = supplier_performance() if perf is None else perf
    row = perf.loc[perf["supplier_id"] == supplier_id]
    try:
        promised_d = (promised if isinstance(promised, date)
                      else date.fromisoformat(str(promised)[:10]))
    except (ValueError, TypeError):
        return {"available": False, "reason": "承諾日無法解析"}

    if row.empty or not bool(row.iloc[0]["樣本是否足夠"]):
        n = int(row.iloc[0]["樣本數"]) if not row.empty else 0
        return {"available": False,
                "reason": f"歷史樣本不足（{n} 筆），不提供保守估計"}

    r = row.iloc[0]
    p80 = int(r["P80 延遲(天)"])
    return {
        "available": True,
        "promised": promised_d.isoformat(),
        "conservative": (promised_d + timedelta(days=max(0, p80))).isoformat(),
        "p80_delay": p80,
        "otd_rate": float(r["準交率"]),
        "n": int(r["樣本數"]),
        "note": (f"歷史 {int(r['樣本數'])} 筆：準交率 {r['準交率']:.0%}，"
                 f"八成的單在承諾日後 {p80} 天內到料"),
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
    perf = supplier_performance(data)
    for sid, promised in [("SUP-F03", "2026-10-29"), ("SUP-S01", "2026-10-14")]:
        r = conservative_eta(sid, promised, perf)
        if r["available"]:
            print(f"  {sid}  承諾 {r['promised']} -> 保守 {r['conservative']}  （{r['note']}）")
        else:
            print(f"  {sid}  {r['reason']}")

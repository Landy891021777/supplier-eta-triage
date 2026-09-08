# -*- coding: utf-8 -*-
"""
效益量化。

===========================  誠實聲明  ===========================
本檔不會產生「節省 30% 人力」這類數字。那種數字沒有根據，
在面試或提案時被追問一句「怎麼算的」就會垮掉。

這裡提供的是**效益公式 + 可調參數 + 敏感度分析**：
  - 公式與程式碼公開，任何人可以檢查算法
  - 參數（人工判讀秒數、每日可追案件數）寫在 config.yaml，隨時可改
  - 輸出敏感度表，顯示在不同假設下結論會怎麼變

引用這些數字時必須連同假設一起引用。
==================================================================

關於 Recall@K 的循環性（必須說明，不可省略）：
    下面用「實際缺口天數 > 0」當作 outcome 來評估排序品質。
    這個 outcome 是由 need_date 與 new_eta 直接算出的客觀事實，
    不是評分卡的輸出 —— 但評分卡的規則 1（緩衝天數，權重 25）
    確實使用了同樣的訊號，因此這個比較對本工具有利。

    它能證明的是：排序邏輯有效地把緩衝訊號傳遞到清單前段。
    它不能證明：工具能預測任何未知的未來結果。
    真實效能必須上線後以 A/B 驗證。
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd


def _to_date(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def add_outcome(actions: pd.DataFrame) -> pd.DataFrame:
    """
    加上客觀 outcome 欄位：實際缺口天數。

    缺口天數 = 新預計到料日 − 下游需求日。
    > 0 代表這張單真的會來不及（實質缺料），這是不需要模型、
    直接從兩個日期相減就能確認的事實。
    """
    df = actions.copy()
    gaps = []
    for _, r in df.iterrows():
        eta = _to_date(r.get("new_eta")) or _to_date(r.get("committed_date"))
        need = _to_date(r.get("need_date"))
        gaps.append((eta - need).days if (eta and need) else np.nan)
    df["shortage_days"] = gaps
    df["will_be_short"] = df["shortage_days"] > 0
    return df


def recall_at_k(df: pd.DataFrame, order_col: str, k: int,
                ascending: bool = False) -> float:
    """依 order_col 排序後，前 k 筆抓到多少比例的『會缺料』案件。"""
    total = int(df["will_be_short"].sum())
    if total == 0:
        return float("nan")
    ordered = df.sort_values(order_col, ascending=ascending, kind="mergesort")
    return float(ordered.head(k)["will_be_short"].sum()) / total


def compare_strategies(df: pd.DataFrame, k: int, n_random: int = 200,
                       seed: int = 20260908) -> pd.DataFrame:
    """
    比較四種排序策略在 Recall@K 上的表現。

    三個 baseline 都是實務上真的會發生的做法，不是刻意做弱的稻草人：
      - FCFS：照收信時間處理，這就是沒有工具時的現況
      - 延遲天數：最直覺的土法，很多人會這樣排
      - 隨機：理論下限
    """
    df = df.copy()
    rng = np.random.default_rng(seed)

    # 現況：照收信順序處理
    df["_fcfs"] = pd.to_datetime(df["received_at"], errors="coerce")

    # 土法：延遲天數最多的先追
    delays = []
    for _, r in df.iterrows():
        eta, com = _to_date(r.get("new_eta")), _to_date(r.get("committed_date"))
        delays.append((eta - com).days if (eta and com) else 0)
    df["_delay_days"] = delays

    rand_scores = []
    for _ in range(n_random):
        df["_rand"] = rng.random(len(df))
        rand_scores.append(recall_at_k(df, "_rand", k))

    rows = [
        {"策略": "本工具（影響分數排序）", "Recall@K": recall_at_k(df, "impact_score", k)},
        {"策略": "土法：依延遲天數排序", "Recall@K": recall_at_k(df, "_delay_days", k)},
        {"策略": "現況：依收信時間 (FCFS)", "Recall@K": recall_at_k(df, "_fcfs", k, ascending=True)},
        {"策略": f"隨機抽查（{n_random} 次平均）", "Recall@K": float(np.nanmean(rand_scores))},
    ]
    out = pd.DataFrame(rows)
    out["Recall@K"] = out["Recall@K"].round(3)
    return out


def compression(stats: dict) -> dict:
    """
    訊息壓縮率：從『一堆信』收斂到『幾件今天要做的事』。

    這是最誠實的一個效益數字 —— 它完全由程式算出，沒有任何假設參數。
    """
    emails = stats.get("emails_processed", 0)
    actionable = stats.get("actionable", 0)
    urgent = stats.get("p1", 0) + stats.get("p2", 0)
    return {
        "收到的信件數": emails,
        "解析出的變更筆數": stats.get("records_extracted", 0),
        "自動濾除（供應商確認無變更）": stats.get("no_change_filtered", 0),
        "進入行動清單": actionable,
        "需今天或本週處理 (P1+P2)": urgent,
        "壓縮率（信件數 → P1+P2）": (round(1 - urgent / emails, 3) if emails else None),
    }


def workload_sensitivity(stats: dict, cfg_benefit: dict) -> pd.DataFrame:
    """
    工時敏感度分析。

    不給單一數字，給一張表 —— 因為「人工判讀一封信要幾分鐘」
    每家公司、每個人都不一樣。使用者應該挑自己認同的那一列來看。
    """
    emails = stats.get("emails_processed", 0)
    actionable = stats.get("p1", 0) + stats.get("p2", 0)
    tool_sec = float(cfg_benefit.get("tool_seconds_per_action", 45))
    workdays = int(cfg_benefit.get("workdays_per_month", 21))

    rows = []
    for manual_sec in (90, 120, 180, 240, 300):
        manual_h = emails * manual_sec / 3600
        tool_h = actionable * tool_sec / 3600
        rows.append({
            "假設：人工判讀一封信（秒）": manual_sec,
            "現況每日工時": round(manual_h, 2),
            "使用工具後每日工時": round(tool_h, 2),
            "每日節省工時": round(manual_h - tool_h, 2),
            "每月節省工時": round((manual_h - tool_h) * workdays, 1),
        })
    return pd.DataFrame(rows)


def full_report(actions: pd.DataFrame, stats: dict, cfg: dict) -> dict:
    b = cfg["benefit"]
    k = int(b["daily_review_capacity"])
    df = add_outcome(actions)
    return {
        "compression": compression(stats),
        "k": k,
        "n_actions": len(df),
        "n_will_be_short": int(df["will_be_short"].sum()),
        "strategies": compare_strategies(df, k),
        "sensitivity": workload_sensitivity(stats, b),
        "scored": df,
    }

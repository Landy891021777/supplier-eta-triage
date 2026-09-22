# -*- coding: utf-8 -*-
"""
時間切分回測：驗證「用過去的落差估計保守到料日」這個方法。

===========================  這驗證什麼、不驗證什麼  ===========================
驗證：
  1. 保守到料日的涵蓋率 —— 實際到貨日落在估計日期以內的比例，
     是否接近設定的百分位（80% 就該約 80%）。
  2. 排序前段的命中率 —— 排在前面的單，實際上真的缺料的比例，
     是否高於「只看供應商說的日期」「只看供應商整體準交率」這兩個簡單做法。

不驗證：
  合成資料由我們自己的因果模型產生，這裡的數字**只能說明方法沒有偷看未來、
  估計有校準**，不能說明真實供應商會照這種分布行動。
  沒贏基準就如實寫沒贏，不調參數讓它好看。

防偷看的做法：每一張測試單，只用「它收到改期通知那天以前就已收貨」的
歷史來估計（見 training_slice）。
=============================================================================
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from supplier_stats import estimate_delay, load_outcomes

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "回測結果.md"

PERCENTILES = (0.80, 0.90, 0.95)
TEST_MONTHS = 4     # 最近幾個月當測試期，之前的歷史只拿來訓練
TOP_FRACTION = 0.2  # 排序前段：取前 20%


def training_slice(outcomes: pd.DataFrame, notice_date) -> pd.DataFrame:
    """
    「通知當天已經知道」的歷史：只含通知日以前已收貨的單。

    通知日之後才收的貨，在當天是未來。把它們算進去，涵蓋率會漂亮得
    不像話，而上線時根本沒有那些資料。
    """
    return outcomes[pd.to_datetime(outcomes["receipt_date"]) < pd.Timestamp(notice_date)]


def rolling_backtest(outcomes: pd.DataFrame, test_from, *, min_samples: int = 20,
                     percentiles=PERCENTILES) -> pd.DataFrame:
    """對測試期內每一張改期過的單，用當時已知的歷史算估計，再對照實際結果。"""
    o = outcomes.copy()
    o["_notice"] = pd.to_datetime(o["notice_date"])
    tests = o[(o["reschedule_count"] >= 1) & (o["_notice"] >= pd.Timestamp(test_from))]

    rows = []
    for _, t in tests.iterrows():
        train = training_slice(outcomes, t["_notice"])
        row = {"po_no": t["po_no"], "supplier_id": t["supplier_id"],
               "delay_days": int(t["delay_days"]),
               "committed": pd.Timestamp(t["committed_date"]),
               "need": pd.Timestamp(t["need_date"]),
               "vendor_otd": float(t["vendor_otd"]),
               "actual_short": bool(pd.Timestamp(t["receipt_date"])
                                    > pd.Timestamp(t["need_date"]))}
        for p in percentiles:
            est = estimate_delay(train, t["supplier_id"], percentile=p,
                                 min_samples=min_samples)
            row[f"est_{int(round(p * 100))}"] = est["delay_days"] if est["available"] else None
        rows.append(row)

    bt = pd.DataFrame(rows)
    if bt.empty:
        return bt
    bt["buffer_days"] = (bt["need"] - bt["committed"]).dt.days
    # 越大越該排前面（越缺）。
    bt["gap_ours"] = bt["est_80"].fillna(0) - bt["buffer_days"]   # 樣本不足時退回只看說定日期
    bt["gap_buffer"] = -bt["buffer_days"]                          # 基準 A：只看緩衝
    bt["gap_vendor"] = 1 - bt["vendor_otd"]                        # 基準 B：只看整體準交率
    return bt


def coverage(bt: pd.DataFrame, percentile: float) -> tuple[float | None, int]:
    """實際到貨落差 ≤ 估計天數的比例；只計有估計的單。"""
    col = f"est_{int(round(percentile * 100))}"
    sub = bt[bt[col].notna()]
    if sub.empty:
        return None, 0
    return float((sub["delay_days"] <= sub[col]).mean()), int(len(sub))


def precision_at_k(bt: pd.DataFrame, score_col: str, k: int,
                   ascending: bool = False) -> float:
    """依 score_col 排序後，前 k 張單真的缺料的比例。"""
    top = bt.sort_values(score_col, ascending=ascending, kind="mergesort").head(k)
    return float(top["actual_short"].mean())


def ranking_table(bt: pd.DataFrame, frac: float = TOP_FRACTION) -> pd.DataFrame:
    k = max(1, int(round(len(bt) * frac)))
    rows = [
        ("本工具：預估缺料天數", precision_at_k(bt, "gap_ours", k)),
        ("基準 A：只看供應商說的日期（緩衝天數）", precision_at_k(bt, "gap_buffer", k)),
        ("基準 B：只看供應商整體準交率（低者優先）", precision_at_k(bt, "gap_vendor", k)),
        ("隨機（等於這批單實際缺料的比例）", float(bt["actual_short"].mean())),
    ]
    out = pd.DataFrame(rows, columns=["方法", "前段命中率"])
    out.attrs["k"] = k
    return out


def run(as_of: date | None = None) -> dict:
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if as_of is None:
        as_of = date.fromisoformat(cfg["data_generation"]["as_of_date"])
    min_samples = int(cfg["triage"]["min_samples"])
    test_from = as_of - timedelta(days=30 * TEST_MONTHS)
    outcomes = load_outcomes()
    bt = rolling_backtest(outcomes, test_from, min_samples=min_samples)
    return {"bt": bt, "test_from": test_from, "as_of": as_of,
            "n_history": len(outcomes)}


def write_report(res: dict, path: Path = OUT) -> str:
    bt = res["bt"]
    cov_rows = []
    for p in PERCENTILES:
        rate, n = coverage(bt, p)
        cov_rows.append((f"P{int(round(p * 100))}", f"{p:.0%}",
                         "—" if rate is None else f"{rate:.1%}", n))
    cov = pd.DataFrame(cov_rows, columns=["百分位", "預期涵蓋率", "實際涵蓋率", "有估計的單數"])
    rank = ranking_table(bt)
    rank["前段命中率"] = rank["前段命中率"].map(lambda v: f"{v:.1%}")
    no_est = int(bt["est_80"].isna().sum())

    text = f"""# 時間切分回測結果

> ⚠️ **合成資料。** 歷史結果由 `src/generate_history.py` 的因果模型產生。
> 這份回測驗證的是**方法**（估計有沒有校準、有沒有偷看未來），
> **不能證明真實供應商會照這種分布行動。**

## 設定

- 歷史已結案單：**{res['n_history']}** 張
- 測試期：通知日 ≥ {res['test_from']}（最近 {TEST_MONTHS} 個月），
  測試單 **{len(bt)}** 張（都是曾改期過的單，其中 {no_est} 張當時樣本不足、退回只看說定日期）
- 防偷看：每張測試單只用「它收到改期通知那天以前就已收貨」的單來估計
- 實際缺料的定義：實際收貨日 > 下游需求日；這批測試單的缺料比例為 **{bt['actual_short'].mean():.1%}**

## 一、保守到料日的涵蓋率

實際到貨日落在估計日期以內的比例，應該接近預期。差太多代表估計不準。

{cov.to_markdown(index=False)}

## 二、排序前段的命中率（前 {int(TOP_FRACTION * 100)}%，共 {rank.attrs['k']} 張）

排在前面的單，實際上真的缺料的比例。**沒贏基準就是沒贏。**

{rank.to_markdown(index=False)}

## 怎麼讀這份結果

- 涵蓋率在測試期沒有偏離預期，只說明「用過去落差估計」這個方法在**有供應商表現隨時間變化**
  的合成資料上仍然站得住腳；那些變化是我們自己寫進產生器的，見 `SUPPLIER_DRIFT`。
- 前段命中率是把測試期的單放在一起排序，不是逐日模擬企劃每天面對的清單。
- 兩個基準都是實務上真的會用的簡單做法，不是刻意做弱的稻草人。
- 基準 A 與本工具的差別只在「有沒有加上這家供應商過去的落差」，
  所以兩者的差距就是歷史資料的貢獻。
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(write_report(run()))
    print(f"\n[OK] 已寫入 {OUT}")

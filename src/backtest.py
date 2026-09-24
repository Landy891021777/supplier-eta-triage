# -*- coding: utf-8 -*-
"""
時間切分回測：驗證「用過去的落差估計保守到料日」這個方法。

===========================  這驗證什麼、不驗證什麼  ===========================
驗證：
  1. 保守到料日的涵蓋率 —— 實際到貨日落在估計日期以內的比例，
     是否接近設定的百分位（80% 就該約 80%）。
  2. 排序前段的命中率 —— 排在前面的單，實際上真的缺料的比例，
     是否高於「只看供應商說的日期」「只看供應商過去準交率」這兩個簡單做法。

不驗證：
  合成資料由我們自己的因果模型產生，這裡的數字**只能說明方法沒有偷看未來、
  估計有校準**，不能說明真實供應商會照這種分布行動。
  沒贏基準就如實寫沒贏，不調參數讓它好看。

防偷看的做法：
  1. 每一張測試單，只用「它收到改期通知那天以前就已收貨」的歷史來估計
     （見 training_slice）。
  2. 測試期本身也不能貼著資料的結尾（as_of）—— 太晚通知改期的單，
     真正晚到的可能還沒收貨、根本還不在歷史庫裡，見 TEST_END_LAG_DAYS。
=============================================================================
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from supplier_stats import estimate_delay, load_outcomes

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "回測結果.md"

PERCENTILES = (0.80, 0.90, 0.95)
TEST_MONTHS = 4     # 最近幾個月當測試期，之前的歷史只拿來訓練
TOP_FRACTION = 0.2  # 排序前段：取前 20%
# 領域假設：歷史上 P99 的落差約 50 天；比這更近的通知，晚到的單可能還沒
# 收貨、根本不在歷史裡，留在測試期會高估涵蓋率。
TEST_END_LAG_DAYS = 60


def training_slice(outcomes: pd.DataFrame, notice_date) -> pd.DataFrame:
    """
    「通知當天已經知道」的歷史：只含通知日以前已收貨的單。

    通知日之後才收的貨，在當天是未來。把它們算進去，涵蓋率會漂亮得
    不像話，而上線時根本沒有那些資料。
    """
    return outcomes[pd.to_datetime(outcomes["receipt_date"]) < pd.Timestamp(notice_date)]


def rolling_backtest(outcomes: pd.DataFrame, test_from, test_to=None, *,
                     min_samples: int = 20, percentiles=PERCENTILES,
                     gr_days_by_category: dict | None = None) -> pd.DataFrame:
    """
    對測試期內每一張改期過的單，用當時已知的歷史算估計，再對照實際結果。

    test_to：測試期上緣（含）。不給就不設上限 —— 只有 run() 會傳，因為只有
    它是對著「現在」(as_of) 算，才需要留 TEST_END_LAG_DAYS 的右尾設限；
    單元測試用固定的合成資料，時間軸是假的，不需要這層保護。

    gr_days_by_category：{料別: 收貨處理天數}，不給就全部視為 0
    （行為與加入收貨處理天數之前完全一樣）。「實際缺料」因此改成
    「收貨日 + 收貨處理天數 > 需求日」——料到廠不代表能投產，
    回測若還是只看收貨日，會把光阻、光罩這種到廠後要處理才能用的料
    看得太樂觀。歷史紀錄沒有企劃的逐料號調整，一律用料別預設。
    """
    gr_days_by_category = gr_days_by_category or {}
    o = outcomes.copy()
    o["_notice"] = pd.to_datetime(o["notice_date"])
    mask = (o["reschedule_count"] >= 1) & (o["_notice"] >= pd.Timestamp(test_from))
    if test_to is not None:
        mask &= (o["_notice"] <= pd.Timestamp(test_to))
    tests = o[mask]

    rows = []
    for _, t in tests.iterrows():
        train = training_slice(outcomes, t["_notice"])
        # 基準 B 的準交率也只能用「通知當時已收貨」的單現算，理由同上：
        # vendor_master 的靜態值是整年平均，包含這張測試單之後才發生的收貨，
        # 拿來評分測試單一樣是偷看未來。
        mine_train = train[train["supplier_id"] == t["supplier_id"]]
        hist_otd = (float((mine_train["delay_days"] <= 0).mean())
                    if len(mine_train) else float("nan"))
        gr = int(gr_days_by_category.get(t.get("category"), 0))
        row = {"po_no": t["po_no"], "supplier_id": t["supplier_id"],
               "delay_days": int(t["delay_days"]),
               "committed": pd.Timestamp(t["committed_date"]),
               "need": pd.Timestamp(t["need_date"]),
               "vendor_otd": float(t["vendor_otd"]),
               "hist_otd": hist_otd,
               "gr_days": gr,
               "actual_short": bool(pd.Timestamp(t["receipt_date"]) + pd.Timedelta(days=gr)
                                    > pd.Timestamp(t["need_date"]))}
        for p in percentiles:
            est = estimate_delay(train, t["supplier_id"], percentile=p,
                                 min_samples=min_samples)
            row[f"est_{int(round(p * 100))}"] = est["delay_days"] if est["available"] else None
        rows.append(row)

    bt = pd.DataFrame(rows)
    if bt.empty:
        return bt
    bt["buffer_days"] = (bt["need"] - bt["committed"]).dt.days - bt["gr_days"]
    # 越大越該排前面（越缺）。一律用 P80：現場工具依承諾強度
    # （confirmed／estimated／intent_only）選 P80／P90／P95，但歷史收貨
    # 紀錄沒有承諾強度這個欄位，回測沒得選，固定用 P80。
    bt["gap_ours"] = bt["est_80"].fillna(0) - bt["buffer_days"]   # 樣本不足時退回只看說定日期
    bt["gap_buffer"] = -bt["buffer_days"]                          # 基準 A：只看緩衝
    # 基準 B：只看這家供應商「通知當時已收貨」的準交率，不是 vendor_master 的
    # 靜態整年平均（vendor_otd 欄位仍保留在表裡，但不可拿它排名）。
    # 領域假設：通知當時完全沒有已收貨紀錄的供應商（hist_otd 是 NaN），
    # 用 -inf 讓它排到最後，而不是回填平均值 —— 沒有歷史時，工具不該
    # 假裝知道排名，寧可讓它墊底、逼人工去看，也不要用平均值悄悄蓋過去。
    bt["gap_vendor"] = (1 - bt["hist_otd"]).fillna(float("-inf"))
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
    """
    依 score_col 排序後，前 k 張單真的缺料的比例；同分（tie）不分先後。

    為什麼要特別處理同分：原本直接排序取前 k 筆，同分的單靠 `sort_values`
    穩定排序保留的原始順序（也就是資料表裡誰先誰後）決定誰落在門檻內、
    誰被擠出去 —— 那個順序跟分數本身無關，等於用一個不相干的欄位偷偷
    決定了勝負，數字會因為資料湊巧的排列而偏高或偏低。正確做法是：
    門檻以上（嚴格比第 k 名的分數更好）全算，門檻正好等於第 k 名分數的
    那一組不分先後，按這組「實際缺料的比例」平分著算。
    """
    n = len(bt)
    if n == 0 or k <= 0:
        return float("nan")
    k = min(k, n)
    s = bt.sort_values(score_col, ascending=ascending, kind="mergesort")
    threshold = s[score_col].iloc[k - 1]
    if ascending:
        strictly_better = bt[bt[score_col] < threshold]
    else:
        strictly_better = bt[bt[score_col] > threshold]
    tied = bt[bt[score_col] == threshold]
    n_above = len(strictly_better)
    n_fill = k - n_above
    above_sum = float(strictly_better["actual_short"].sum())
    tied_rate = float(tied["actual_short"].mean()) if len(tied) else 0.0
    return float((above_sum + n_fill * tied_rate) / k)


def auc(bt: pd.DataFrame, col: str) -> float:
    """
    P(隨機一張缺料單的分數 > 隨機一張沒缺料單的分數)，同分算一半。

    跟 precision_at_k 互補：那個只看「前 k 名選得準不準」（受 k 怎麼選
    影響），這個看整條排序線的品質，跟 k 無關。任一類別是空的就沒有
    「比較」可言，回傳 NaN 而不是假裝算得出一個數字。
    """
    y = bt["actual_short"].to_numpy()
    if y.sum() == 0 or y.sum() == len(y):
        return float("nan")
    return float(roc_auc_score(y, bt[col].to_numpy()))


def bootstrap_diff(bt: pd.DataFrame, col_a: str, col_b: str, k: int,
                   n: int = 2000, seed: int = 0) -> tuple[float, float, float]:
    """
    兩個排序欄位在前段命中率的差距（col_a − col_b）的 95% 區間，重抽樣估計。

    為什麼：測試單通常只有幾十到一百多張，單一個命中率數字的抽樣誤差
    很大，「贏了 5 個百分點」在這種樣本數下可能只是雜訊。區間比單一
    數字誠實 —— 區間跨過 0 就代表現有這批測試單不夠分出高下。固定亂數
    種子（預設 0）讓同一份資料每次重跑都得到一樣的區間，報告裡的數字
    才能被覆核。
    """
    rng = np.random.default_rng(seed)
    m = len(bt)
    diffs = np.empty(n)
    for i in range(n):
        idx = rng.integers(0, m, size=m)
        sample = bt.iloc[idx]
        diffs[i] = precision_at_k(sample, col_a, k) - precision_at_k(sample, col_b, k)
    low, high = (float(x) for x in np.quantile(diffs, [0.025, 0.975]))
    share_a_better = float((diffs > 0).mean())
    return low, high, share_a_better


def ranking_table(bt: pd.DataFrame, frac: float = TOP_FRACTION) -> tuple[pd.DataFrame, int]:
    """回傳（表格, k）。k 明確回傳而不是塞進 DataFrame.attrs —— attrs 在
    DataFrame 被複製、篩選、to_markdown 之後很容易悄悄消失，呼叫端還不知道。"""
    k = max(1, int(round(len(bt) * frac)))
    rows = [
        ("本工具：預估缺料天數（一律用 P80）",
         precision_at_k(bt, "gap_ours", k), auc(bt, "gap_ours")),
        ("基準 A：只看供應商說的日期（緩衝天數）",
         precision_at_k(bt, "gap_buffer", k), auc(bt, "gap_buffer")),
        ("基準 B：只看這家供應商過去已收貨單的準交率（低者優先）",
         precision_at_k(bt, "gap_vendor", k), auc(bt, "gap_vendor")),
        ("隨機（等於這批單實際缺料的比例）", float(bt["actual_short"].mean()), float("nan")),
    ]
    out = pd.DataFrame(rows, columns=["方法", "前段命中率", "AUC"])
    return out, k


def run(as_of: date | None = None) -> dict:
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if as_of is None:
        as_of = date.fromisoformat(cfg["data_generation"]["as_of_date"])
    # Task 5 才會在 config.yaml 加 triage 區塊；在那之前這裡要能跑，
    # 所以沒有這個區塊就退回跟 estimate_delay 預設值一致的 20。
    min_samples = int(cfg.get("triage", {}).get("min_samples", 20))
    # 領域假設：歷史沒有企劃的逐料號調整，回測一律用料別預設
    # （config.yaml 的 receiving.gr_processing_days）。
    gr_days_by_category = cfg.get("receiving", {}).get("gr_processing_days", {})
    test_to = as_of - timedelta(days=TEST_END_LAG_DAYS)
    test_from = test_to - timedelta(days=30 * TEST_MONTHS)
    outcomes = load_outcomes()
    bt = rolling_backtest(outcomes, test_from, test_to, min_samples=min_samples,
                          gr_days_by_category=gr_days_by_category)
    return {"bt": bt, "test_from": test_from, "test_to": test_to, "as_of": as_of,
            "n_history": len(outcomes)}


COVERAGE_TOLERANCE = 0.03  # 實際與預期相差 3 個百分點以內，視為沒有偏離


def coverage_verdict(bt: pd.DataFrame) -> str:
    """
    依實際數字寫出涵蓋率的結論，不寫死。

    踩坑紀錄：這句話原本寫死成「涵蓋率沒有偏離預期」。換成晶圓廠資料後，
    P95 只涵蓋 90%，那句話就變成不實陳述，而報告照樣產出、不會報錯。
    """
    off = []
    for p in PERCENTILES:
        rate, n = coverage(bt, p)
        if rate is not None and abs(rate - p) > COVERAGE_TOLERANCE:
            off.append((p, rate))
    if not off:
        return ("各百分位的實際涵蓋率都在預期 ±3 個百分點內：用過去落差估計的方法，"
                "在**有供應商表現隨時間變化**的合成資料上仍然站得住腳；那些變化是我們"
                "自己寫進產生器的，見 `SUPPLIER_DRIFT`。")
    parts = "、".join(f"P{int(round(p * 100))} 實際 {r:.1%}（預期 {p:.0%}）" for p, r in off)
    low = any(r < p for p, r in off)
    why = ("低於預期代表估計偏樂觀。可能的原因之一是供應商表現隨時間變差"
           "（`SUPPLIER_DRIFT`），而估計用的是變差前後混在一起的整段歷史；"
           "這是「只看歷史統計、不做預測」這個設計的已知代價。"
           if low else "高於預期代表估計偏保守，保守到料日會比實際晚。")
    return f"涵蓋率有偏離：{parts}。{why}"


def write_report(res: dict, path: Path = OUT) -> str:
    bt = res["bt"]
    test_from = res["test_from"]
    test_to = res.get("test_to")
    n_history = res["n_history"]

    if bt.empty:
        text = f"""# 時間切分回測結果

> ⚠️ **合成資料。** 歷史結果由 `src/generate_history.py` 的因果模型產生。

測試期（通知日介於 {test_from} 至 {test_to}）內沒有改期過的單，無法計算涵蓋率或排序命中率。

- 歷史已結案單：**{n_history}** 張
"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text

    cov_rows = []
    for p in PERCENTILES:
        rate, n = coverage(bt, p)
        cov_rows.append((f"P{int(round(p * 100))}", f"{p:.0%}",
                         "—" if rate is None else f"{rate:.1%}", n))
    cov = pd.DataFrame(cov_rows, columns=["百分位", "預期涵蓋率", "實際涵蓋率", "有估計的單數"])
    cov_note = coverage_verdict(bt)
    rank, k = ranking_table(bt)
    rank["前段命中率"] = rank["前段命中率"].map(lambda v: f"{v:.1%}")
    rank["AUC"] = rank["AUC"].map(lambda v: "—" if pd.isna(v) else f"{v:.3f}")
    no_est = int(bt["est_80"].isna().sum())
    lo, hi, share = bootstrap_diff(bt, "gap_ours", "gap_buffer", k)

    text = f"""# 時間切分回測結果

> ⚠️ **合成資料。** 歷史結果由 `src/generate_history.py` 的因果模型產生。
> 這份回測驗證的是**方法**（估計有沒有校準、有沒有偷看未來），
> **不能證明真實供應商會照這種分布行動。**

## 設定

- 歷史已結案單：**{n_history}** 張
- 測試期：通知日介於 {test_from} 至 {test_to}（{TEST_MONTHS} 個月的窗，
  結尾往前留 {TEST_END_LAG_DAYS} 天不用）；
  測試單 **{len(bt)}** 張（都是曾改期過的單，其中 {no_est} 張當時樣本不足、退回只看說定日期）
- 為什麼結尾要留 {TEST_END_LAG_DAYS} 天：太接近現在（as_of）才通知改期的單，
  真正晚到的可能還沒收貨、根本還不在歷史庫裡（右尾設限），留在測試期
  會讓涵蓋率看起來比實際好
- 防偷看：每張測試單只用「它收到改期通知那天以前就已收貨」的單來估計
- 實際缺料的定義：實際收貨日＋收貨處理天數 > 下游需求日；
  這批測試單的缺料比例為 **{bt['actual_short'].mean():.1%}**
- 收貨處理天數用料別預設（config.yaml 的 `receiving.gr_processing_days`）；
  歷史沒有企劃的逐料號調整

## 一、保守到料日的涵蓋率

實際到貨日落在估計日期以內的比例，應該接近預期。差太多代表估計不準。

{cov.to_markdown(index=False)}

## 二、排序前段的命中率（前 {int(TOP_FRACTION * 100)}%，共 {k} 張）

排在前面的單，實際上真的缺料的比例。**沒贏基準就是沒贏。**
AUC 是整體排序品質（隨機一張缺料單分數高於一張沒缺料單的機率），跟 k 怎麼選無關。

{rank.to_markdown(index=False)}

本工具 − 基準 A 的前段命中率差，95% 區間 [{lo:.1%}, {hi:.1%}]（bootstrap 重抽樣，
{share:.0%} 的重抽樣本工具較高）

## 怎麼讀這份結果

- {cov_note}
- 前段命中率是把測試期的單放在一起排序，不是逐日模擬企劃每天面對的清單。
- 兩個基準都是實務上真的會用的簡單做法，不是刻意做弱的稻草人。
- 本工具與基準 A 的差別只在「有沒有加上這家供應商過去的落差」；
  若上面的區間跨過 0，代表這批測試單不足以分出高下。
- 現場工具依承諾強度（confirmed／estimated／intent_only）選 P80／P90／P95；
  歷史收貨紀錄沒有承諾強度這個欄位，回測沒得選，一律用 P80。
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(write_report(run()))
    print(f"\n[OK] 已寫入 {OUT}")

# -*- coding: utf-8 -*-
"""
回測的測試。守住的是：**不可以偷看未來**（訓練資料、測試期兩層都不行），
以及排序評分不能靠巧合（同分的處理方式、抽樣誤差都要誠實）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import backtest  # noqa: E402


def _row(po, sup, delay, resched, committed, receipt, need, notice, otd=0.85,
        category="TARGET"):
    return {"po_no": po, "supplier_id": sup, "delay_days": delay,
            "reschedule_count": resched, "committed_date": committed,
            "receipt_date": receipt, "need_date": need,
            "notice_date": notice, "vendor_otd": otd, "category": category}


def _toy() -> pd.DataFrame:
    """A 供應商：上半年 30 張改期單全都準時；下半年才開始變糟（延 20 天）。"""
    rows = [_row(f"E{i}", "A", 0, 1, "2026-01-20", "2026-01-20", "2026-02-20",
                 "2026-01-10") for i in range(30)]
    rows += [_row(f"L{i}", "A", 20, 1, "2026-07-01", "2026-07-21", "2026-08-01",
                  "2026-06-20") for i in range(30)]
    # M1：通知日在 T1 之前，但「收貨日」晚到 2026-05-15 —— 剛好卡在 T1 的
    # 通知日（5/1）之後、T1 自己的收貨日（5/30）之前。若訓練集是照通知日
    # 而不是照收貨日篩，這張單會被誤判成「T1 通知當時已經知道結果」而
    # 漏進訓練集，這就是偷看未來。
    rows.append(_row("M1", "A", 20, 1, "2026-04-25", "2026-05-15", "2026-05-20",
                     "2026-04-20"))
    # 唯一的測試單：通知日 2026-05-01，此時只知道上半年那些全準時的單
    rows.append(_row("T1", "A", 20, 1, "2026-05-10", "2026-05-30", "2026-05-25",
                     "2026-05-01"))
    return pd.DataFrame(rows)


def test_training_slice_only_contains_orders_already_received():
    df = _toy()
    train = backtest.training_slice(df, "2026-05-01")
    assert (pd.to_datetime(train["receipt_date"]) < pd.Timestamp("2026-05-01")).all()
    assert "T1" not in set(train["po_no"])
    assert "M1" not in set(train["po_no"])
    assert not any(p.startswith("L") for p in train["po_no"])


def test_rolling_backtest_does_not_see_the_future():
    """
    回歸測試（防偷看）：T1 在 5/1 收到改期通知，當時 A 供應商過去的單全都
    準時，估計應為 0 天。若誤把下半年那 30 張延 20 天的單、或 M1 那張
    收貨日晚於通知日的單算進去，估計會變得不是 0 —— 涵蓋率因此看起來
    很漂亮，但那是作弊。
    """
    bt = backtest.rolling_backtest(_toy(), test_from="2026-05-01", min_samples=20)
    t1 = bt[bt["po_no"] == "T1"]
    assert len(t1) == 1
    assert t1.iloc[0]["est_80"] == 0


def test_rolling_backtest_excludes_orders_noticed_after_test_to():
    """
    test_to 存在的理由：太接近 as_of 才通知改期的單，真正晚到的可能還沒
    收貨、根本不在歷史庫裡（右尾設限），納入測試期會高估涵蓋率。
    這裡直接測邊界會被濾掉，不必等到套進真實資料庫才發現漏了這一層。
    """
    bt = backtest.rolling_backtest(_toy(), test_from="2026-01-01", test_to="2026-04-01",
                                   min_samples=20)
    assert "T1" not in set(bt["po_no"])
    assert "M1" not in set(bt["po_no"])
    assert not any(p.startswith("L") for p in bt["po_no"])
    assert all(p.startswith("E") for p in bt["po_no"])


def test_coverage_counts_only_orders_with_an_estimate():
    bt = pd.DataFrame({"delay_days": [0, 5, 30], "est_80": [4, 4, None],
                       "est_90": [4, 4, None], "est_95": [4, 4, None]})
    rate, n = backtest.coverage(bt, 0.80)
    assert n == 2 and rate == 0.5


def test_precision_at_k_uses_the_ranking_given():
    bt = pd.DataFrame({"gap_ours": [9, 1, 5, 3], "actual_short": [True, False, True, False]})
    assert backtest.precision_at_k(bt, "gap_ours", k=2) == 1.0
    assert backtest.precision_at_k(bt, "gap_ours", k=2, ascending=True) == 0.0


def test_precision_at_k_splits_ties_evenly():
    """
    同分的單本來就說不出誰該排前面，不該靠資料原始順序偷偷決定勝負。
    兩張並列第一（一張缺料、一張沒缺），各算一半才誠實；
    若用穩定排序取前 k，兩張的順序只取決於資料表裡誰先誰後，結果會偏。
    """
    bt = pd.DataFrame({"score": [10, 10, 5, 1], "actual_short": [True, False, True, False]})
    assert backtest.precision_at_k(bt, "score", k=1) == 0.5


def test_bootstrap_diff_is_deterministic_and_ordered():
    """
    固定種子必須重現同一組數字，否則同一份報告兩次執行會給出不同區間，
    沒辦法在文件裡寫死一個數字讓人核對。
    """
    bt = pd.DataFrame({"gap_ours": [9, 1, 5, 3, 7, 2, 6, 4],
                       "gap_buffer": [1, 9, 3, 5, 2, 7, 4, 6],
                       "actual_short": [True, False, True, False, True, False, True, False]})
    r1 = backtest.bootstrap_diff(bt, "gap_ours", "gap_buffer", k=2, n=200, seed=0)
    r2 = backtest.bootstrap_diff(bt, "gap_ours", "gap_buffer", k=2, n=200, seed=0)
    assert r1 == r2
    lo, hi, share = r1
    assert lo <= hi
    assert 0.0 <= share <= 1.0


def test_auc_of_perfect_and_reversed_ranking():
    """AUC 是排序品質本身的檢查：完美排序要是 1.0，完全反過來要是 0.0，
    不然這個指標的實作就是錯的。"""
    perfect = pd.DataFrame({"score": [4, 3, 2, 1], "actual_short": [True, True, False, False]})
    assert backtest.auc(perfect, "score") == 1.0
    reversed_ = pd.DataFrame({"score": [1, 2, 3, 4], "actual_short": [True, True, False, False]})
    assert backtest.auc(reversed_, "score") == 0.0


def test_ranking_table_always_reports_the_random_baseline():
    bt = pd.DataFrame({"gap_ours": [3, 2, 1, 0], "gap_buffer": [0, 1, 2, 3],
                       "gap_vendor": [0.1, 0.2, 0.3, 0.4],
                       "actual_short": [True, False, True, False]})
    t, k = backtest.ranking_table(bt, frac=0.5)
    assert k == 2
    assert any("隨機" in s for s in t["方法"])
    assert t["前段命中率"].between(0, 1).all()
    assert "AUC" in t.columns


def test_baseline_b_uses_history_not_the_static_vendor_master_value():
    """
    基準 B 若讀 vendor_master 的靜態準交率，等於用「整年平均」去評分通知
    當下的排名 —— 那個平均本身是用整年資料算出來的，包含測試單之後才
    發生的收貨，一樣是偷看未來。改成只用「通知當時已收貨」的單現算，
    排名才會照歷史實況走，不是照產生器設定的整體平均走。這裡刻意讓
    兩者相反：P 的靜態值很差、但通知當時的實際紀錄全準時；Q 相反。
    """
    def r(po, sup, delay, committed, receipt, need, notice, static_otd):
        return {"po_no": po, "supplier_id": sup, "delay_days": delay,
                "reschedule_count": 1, "committed_date": committed,
                "receipt_date": receipt, "need_date": need,
                "notice_date": notice, "vendor_otd": static_otd}

    rows = []
    # P：vendor_master 靜態值很差（0.10），但通知當時已收貨的紀錄全部準時。
    rows += [r(f"P{i}", "P", 0, "2026-01-05", "2026-01-05", "2026-02-05",
              "2026-01-01", 0.10) for i in range(25)]
    rows.append(r("P_TEST", "P", 0, "2026-03-01", "2026-03-01", "2026-04-01",
                 "2026-02-25", 0.10))
    # Q：vendor_master 靜態值很好（0.95），但通知當時已收貨的紀錄全部延遲。
    rows += [r(f"Q{i}", "Q", 15, "2026-01-05", "2026-01-20", "2026-01-25",
              "2026-01-01", 0.95) for i in range(25)]
    rows.append(r("Q_TEST", "Q", 15, "2026-03-01", "2026-03-16", "2026-03-10",
                 "2026-02-25", 0.95))
    df = pd.DataFrame(rows)

    bt = backtest.rolling_backtest(df, test_from="2026-02-01", min_samples=20)
    p_gap = bt.loc[bt["supplier_id"] == "P", "gap_vendor"].iloc[0]
    q_gap = bt.loc[bt["supplier_id"] == "Q", "gap_vendor"].iloc[0]
    # 歷史上 P 準時、Q 常遲，基準 B 該把 Q 排在更前面（gap 更大）；
    # 若誤用靜態的 vendor_otd，順序會整個反過來。
    assert q_gap > p_gap


def test_gr_processing_days_affects_actual_short_and_buffer_days():
    """
    光阻到廠要 2 天檢驗＋回溫才能投產。receipt = need − 1 表面上沒缺料，
    但「實際缺料」的定義要看可投產日，不是收貨當天；沒有傳
    gr_days_by_category 時，行為必須跟現在完全一樣（既有回測不能被悄悄
    改變），加了之後才會把這張單判成缺料，buffer_days 也要扣掉這幾天。
    """
    rows = [_row(f"E{i}", "A", 0, 1, "2026-01-20", "2026-01-20", "2026-02-20",
                "2026-01-10", category="PHOTORESIST") for i in range(25)]
    rows.append(_row("T1", "A", 0, 1, "2026-05-01", "2026-05-24", "2026-05-25",
                     "2026-04-20", category="PHOTORESIST"))
    df = pd.DataFrame(rows)

    no_gr = backtest.rolling_backtest(df, test_from="2026-04-01", min_samples=20)
    t1 = no_gr[no_gr["po_no"] == "T1"].iloc[0]
    assert bool(t1["actual_short"]) is False
    assert int(t1["buffer_days"]) == 24

    with_gr = backtest.rolling_backtest(df, test_from="2026-04-01", min_samples=20,
                                        gr_days_by_category={"PHOTORESIST": 2})
    t1_gr = with_gr[with_gr["po_no"] == "T1"].iloc[0]
    assert bool(t1_gr["actual_short"]) is True
    assert int(t1_gr["buffer_days"]) == 22


def test_write_report_handles_empty_backtest(tmp_path):
    """
    測試期內剛好沒有改期單時不該噴例外 —— 切分視窗一縮小、或某段時間剛好
    沒人改期，這是真的會發生的情況，不該讓報表產生器崩潰。
    """
    res = {"bt": pd.DataFrame(), "test_from": "2026-01-01", "test_to": "2026-02-01",
           "n_history": 0}
    out_path = tmp_path / "report.md"
    text = backtest.write_report(res, path=out_path)
    assert "沒有改期過的單" in text
    assert out_path.exists()


def test_coverage_verdict_follows_the_numbers():
    """
    回歸：報告原本寫死「涵蓋率沒有偏離預期」。換成晶圓廠資料後 P95 只涵蓋 90%，
    那句話變成不實陳述、報告照樣產出。結論必須依實際數字寫。
    """
    good = pd.DataFrame({"delay_days": list(range(100)),
                         "est_80": [79] * 100, "est_90": [89] * 100, "est_95": [94] * 100})
    assert "都在預期" in backtest.coverage_verdict(good)
    bad = good.assign(est_95=[89] * 100)
    verdict = backtest.coverage_verdict(bad)
    assert "有偏離" in verdict and "P95" in verdict and "偏樂觀" in verdict

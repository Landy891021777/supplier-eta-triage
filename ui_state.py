# -*- coding: utf-8 -*-
"""
各頁面共用的資料載入、分級套用與側邊欄。

===========================  為什麼獨立成檔  ===========================
介面拆成 `views/*.py` 之後，每個頁面檔案都要能被 `st.navigation` 個別
執行，也要能被測試個別開啟（見 tests/test_app_pages.py）。資料載入、
快取、套用企劃覆寫與確認交期的邏輯如果每頁各寫一份，遲早會有一頁忘記
更新而跟其他頁對不上——所以只寫一份，放在這裡讓每頁 `import ui_state`。

`context()` 是每頁進來第一件事要做的事：確保資料存在、讀 LLM 開關、
跑一次主流程（含快取）、套用企劃在「收貨處理天數」與「確認交期」
兩個功能存的覆寫，回傳畫面要用的所有東西。分級只有這一套邏輯
（`pipeline.retriage()`），不會有兩頁各自算一次而算出不同答案。
=========================================================================
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import pipeline  # noqa: E402
import planner_settings  # noqa: E402
from llm.provider import get_provider  # noqa: E402

PRIORITY_COLOR = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "待查": "⚪", "—": "⚫"}

# 公開展示時的保護：每位訪客在一次工作階段內最多觸發的「未快取」LLM 呼叫次數。
# 超過後改為直接列出檢索到的原始資料／規則式草稿，工具照樣可用，但不再消耗
# API 配額。行動清單的回信草稿與物料智能檢索共用同一個額度計數。
LLM_CALLS_PER_SESSION = int(os.getenv("LLM_CALLS_PER_SESSION", "20"))


@st.cache_data(show_spinner="解析信件中…")
def load_pipeline(use_llm: bool):
    """解析結果快取：介面互動不會觸發重跑，避免重複呼叫 LLM。"""
    return pipeline.run(use_llm=use_llm)


def _ensure_data() -> None:
    """
    首次啟動時自動產生合成資料。

    `data/` 被 .gitignore 排除（它是可重新生成的產物，不該進版控），
    因此雲端部署後第一次啟動會找不到資料。與其要求使用者先跑一次
    指令，不如讓工具自己補上 —— 導入的第一步不該卡在環境設定。

    實際邏輯在 src/bootstrap.py。踩坑紀錄：原本用「資料庫檔案存在」當作
    「準備好了」，雲端首次載入時兩個工作階段重疊，第二個會撞上建到一半的
    資料庫而整個打不開；也曾因為只建了資料庫沒補歷史，讓三個分頁整片空白。
    """
    import bootstrap
    bootstrap.ensure_data(ROOT, step=st.spinner)


def sidebar() -> None:
    """
    側邊欄：LLM 狀態、開關、資料來源。

    只在 app.py 呼叫一次（`st.navigation` 底下，側邊欄是所有頁面共用的
    外殼，不屬於任一頁）。LLM 開關的值寫進 `st.session_state["use_llm"]`
    （由 `st.sidebar.toggle` 的 `key` 參數自動處理），每一頁的
    `context()` 再從 session_state 讀出來，不必把值一路傳參數。

    K 參數（效益試算的每日可追案件數）不在這裡：它只影響「效益量化」
    一頁，放在該頁頂端讓人一眼看到「這個數字只影響這裡」（Plan 2 Task 5
    會正式定案；Task 3 先把它搬到頁面內）。
    """
    cfg = pipeline.load_config()
    st.sidebar.title("⚙️ 設定")

    provider = get_provider()
    if provider.available:
        st.sidebar.success(f"LLM 已連線：{provider.name} / {provider.model}")
    else:
        st.sidebar.warning(
            "**未偵測到 LLM 金鑰 — 目前為僅規則層模式**\n\n"
            "工具仍可完整執行，但自然語言敘述型的信件（相對日期、"
            "模糊措辭）解析會失敗，這是預期行為。\n\n"
            "接上金鑰：複製 `.env.example` 為 `.env` 並填入金鑰。"
        )
    st.sidebar.toggle("啟用 LLM 升級解析", value=provider.available,
                      disabled=not provider.available, key="use_llm")

    # 使用者有權知道畫面上的數字是從哪裡來的。
    st.sidebar.caption(f"**資料來源**：{pipeline.get_data_source(cfg).describe()}")


def context() -> dict:
    """
    每一頁進來第一件事：確保資料 → 讀 LLM 開關 → 跑主流程 → 套用覆寫與確認。

    回傳 dict(result=..., actions=..., stats=..., cfg=..., provider=...,
    use_llm=...)：
      - result：pipeline.run() 的完整回傳（含 traces、as_of、原始 all）。
      - actions：套用「收貨處理天數」覆寫與「企劃確認交期」之後、
        已排序好的行動清單（等同原本 app.py main() 裡的 actions）。
      - stats：result["stats"] 疊上 actions 重算後的 actionable/p1/p2/p3
        （覆寫可能改變分級，數字要跟 actions 對得上，不能沿用 run() 當下
        還沒套覆寫的舊統計）。
    """
    _ensure_data()
    cfg = pipeline.load_config()
    provider = get_provider()
    use_llm = st.session_state.get("use_llm", provider.available)

    try:
        result = load_pipeline(use_llm)
    except FileNotFoundError as e:
        st.error(f"{e}")
        st.stop()

    # ---------------- 套用企劃存的覆寫與確認 ----------------
    # run() 只算出「沒有覆寫、沒有確認」的分級（等同傳空字典給
    # retriage()）。這裡拿 result["all"]（所有已對位的原始欄位）重跑
    # retriage()：只重算分級，不重跑讀信，企劃調完天數或登錄確認、
    # 畫面 rerun 後就能立刻看到新的優先序。
    overrides = planner_settings.load_overrides()
    gr_map = {mid: planner_settings.effective_gr_days(mid, None, None, overrides)
             for mid in overrides}
    confirmations = planner_settings.load_confirmations()
    # result 沒有 "all"（沒有任何信件被解析出結果時 run() 提早回傳）就沒有
    # 東西可以重算，退回原本（同樣是空的）actions。
    actions = pipeline.retriage(result.get("all", result["actions"]), gr_map, cfg["triage"],
                                confirmations=confirmations)

    stats = dict(result["stats"])
    stats.update(
        actionable=len(actions),
        p1=int((actions["priority"] == "P1").sum()),
        p2=int((actions["priority"] == "P2").sum()),
        p3=int((actions["priority"] == "P3").sum()),
    )

    return dict(result=result, actions=actions, stats=stats, cfg=cfg,
               provider=provider, use_llm=use_llm)


def llm_budget_left() -> int:
    """本次工作階段還剩多少「未快取」LLM 呼叫額度（回信草稿與物料智能檢索共用）。"""
    return LLM_CALLS_PER_SESSION - st.session_state.get("llm_live_calls", 0)


@st.cache_resource(show_spinner="建立物料檢索索引…")
def load_retriever():
    from rag.knowledge import build_cards
    from rag.retriever import HybridRetriever
    retriever = HybridRetriever(build_cards(), provider=get_provider())
    retriever.build_semantic_index()
    return retriever


@st.cache_data(show_spinner=False)
def supplier_performance():
    import supplier_stats
    return supplier_stats.supplier_performance()


@st.cache_data(show_spinner=False)
def reschedule_reliability():
    import supplier_stats
    return supplier_stats.reschedule_reliability()


@st.cache_data(show_spinner=False)
def materials_df():
    return pipeline.get_data_source(pipeline.load_config()).materials()


@st.cache_data(show_spinner=False)
def outcomes_df():
    """
    歷史收貨結果（含供應商名稱），供「供應商月度績效」使用。

    supplier_stats.load_outcomes() 本身沒有供應商名稱（只有 supplier_id）；
    月度績效表要給企劃看，不能只顯示代號，所以在這裡併入一次，不必讓
    exports.supplier_monthly() 也認得資料來源怎麼取名稱——它只管統計。

    快取的理由跟 supplier_performance／reschedule_reliability 一樣：
    這支要掃整個 goods_receipt 相關的 JOIN，不該每次切換月份選單就重算一次。
    找不到歷史資料（FileNotFoundError／RuntimeError）不在這裡擋，讓呼叫端
    決定要顯示什麼提示——跟頁面上其他歷史資料的錯誤處理一致。
    """
    import supplier_stats
    outcomes = supplier_stats.load_outcomes().copy()
    sups = pipeline.get_data_source(pipeline.load_config()).suppliers()
    names = sups.set_index("supplier_id")["supplier_name"]
    outcomes["supplier_name"] = outcomes["supplier_id"].map(names)
    return outcomes


def submit_confirmation(po_no: str, email_id: str, confirmed_date: str, note: str,
                        user: str, *, db=None, now: str | None = None) -> tuple[bool, str]:
    """
    包一層給表單 callback／測試共用。

    行動清單頁的確認表單與測試都呼叫這支，而不是各自直接呼叫
    planner_settings.confirm_eta：表單要把 ValueError 轉成 (False, 訊息)
    給 st.error 顯示，測試則想繞過巢狀 st.expander／st.form 裡的元件、
    直接驗證「送出確認」這個動作本身的效果（見
    tests/test_app_pages.py 的說明）。
    """
    try:
        planner_settings.confirm_eta(db, po_no, email_id, confirmed_date, note, user, now=now)
    except ValueError as e:
        return False, str(e)
    return True, ""


def fmt_gap(gap) -> str:
    """預估缺料天數的人話：缺 N 天／沒有緩衝／尚有 N 天緩衝。"""
    if gap is None or gap != gap:
        return "無法估計"
    g = int(gap)
    if g > 0:
        return f"預估缺料 {g} 天"
    if g == 0:
        return "沒有緩衝"
    return f"尚有 {-g} 天緩衝"

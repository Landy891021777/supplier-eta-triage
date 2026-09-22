# -*- coding: utf-8 -*-
"""
🔎 物料智能檢索 —— 用平常講話的方式查詢採購單、料號、供應商表現與工具說明。

依資料形狀切塊＋摘要卡，語意檢索＋識別碼釘選，回答只根據檢索到的資料，
每個事實都標註出處；資料裡沒有的，直接說沒有。
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import ui_state  # noqa: E402
from llm.provider import get_provider  # noqa: E402
from rag.answer import EXAMPLE_QUESTIONS as RAG_EXAMPLES  # noqa: E402
from rag.answer import answer as rag_answer  # noqa: E402

RETRIEVAL_LABEL = {"pinned": "識別碼精確比對", "semantic": "語意相近",
                   "lexical": "字詞相符", "hybrid": "綜合排序"}

ctx = ui_state.context()

st.subheader("物料智能檢索")
st.caption(
    "用平常講話的方式查詢採購單、料號、供應商表現與工具使用說明。"
    "回答只根據檢索到的資料，每一個事實都標註出處；資料裡沒有的，會直接說沒有。")

try:
    retriever = ui_state.load_retriever()
except (FileNotFoundError, RuntimeError, ValueError) as e:
    st.error(f"無法建立檢索索引：{e}")
    st.stop()

mode_label = ("語意檢索 ＋ 識別碼精確比對" if retriever.semantic_ready
              else "詞彙檢索 ＋ 識別碼精確比對（未設定 embedding 金鑰時的備援模式）")
st.caption(f"檢索模式：**{mode_label}**｜知識庫 **{len(retriever.cards)}** 張卡片"
           "（採購單、料號、供應商、摘要與工具文件）")

cols = st.columns(3)
for i, example in enumerate(RAG_EXAMPLES):
    if cols[i % 3].button(example, key=f"rag-ex-{i}", width="stretch"):
        st.session_state["rag_question"] = example

question = st.text_input("輸入問題", key="rag_question",
                         placeholder="例：PR-ArF-1088 有沒有第二家可以買？")

if question:
    budget = ui_state.llm_budget_left()
    provider = get_provider() if budget > 0 else None
    with st.spinner("檢索並整理回答中…"):
        res = rag_answer(question, retriever, provider,
                         reference_date=str(ctx["result"]["as_of"]))

    if res.get("llm"):
        # 只有真的打到 API 才扣額度；命中快取的回答不算
        if res.get("latency_ms", 0) > 0:
            st.session_state["llm_live_calls"] = st.session_state.get("llm_live_calls", 0) + 1
        st.markdown(res["answer"])
        cites = res.get("citations") or {}
        if cites.get("invalid"):
            st.warning(
                f"模型引用了未提供給它的資料 {cites['invalid']}，相關敘述可能是推測，"
                "請以下方原始資料為準。")
        elif cites.get("uncited") and "沒有這項資訊" not in res["answer"]:
            st.warning("回答未標註任何出處，請以下方原始資料為準。")
    else:
        if budget <= 0:
            st.info("本次工作階段的 AI 回答額度已用完，以下直接列出檢索到的原始資料。")
        elif res.get("note"):
            st.info(res["note"])

    if res.get("hits"):
        st.markdown("**參考資料**")
        for hit in res["hits"]:
            label = RETRIEVAL_LABEL.get(hit.source, hit.source)
            with st.expander(f"[{hit.card.card_id}] {hit.card.title}　·　{label}",
                             expanded=not res.get("llm")):
                st.text(hit.card.text)

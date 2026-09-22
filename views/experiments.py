# -*- coding: utf-8 -*-
"""
🧪 評估實驗 —— 每一個設計決定都要有證據，而且評估必須容許推翻原本的假設。

暫時頁面：Plan 2 Task 5 會拆成「專案簡介」與「方法驗證」兩頁，
本頁屆時會被刪除，本 Task 只做搬移，內容與行為不變。
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

# 本頁內容只讀 output/ 底下的既有報告，不需要 actions；仍呼叫 context()
# 是為了讓本頁跟其他頁一樣可以獨立執行（AppTest 直接開這一頁）並確保資料就緒。
ui_state.context()

st.subheader("評估實驗")
st.caption("每一個設計決定都要有證據，而且評估必須容許推翻原本的假設。")
exp_parse, exp_rag = st.tabs(["解析：規則層 vs LLM 層", "檢索：六種配置比較"])
for container, fname, cmd in (
    (exp_parse, "實驗結果.md", "py src/evaluate.py"),
    (exp_rag, "RAG檢索評估.md", "py src/rag/evaluate_rag.py"),
):
    with container:
        path = ROOT / "output" / fname
        if path.exists():
            st.markdown(path.read_text(encoding="utf-8"))
        else:
            st.info(f"尚未執行評估。請於終端機執行： `{cmd}`")

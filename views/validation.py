# -*- coding: utf-8 -*-
"""
🧪 方法驗證 —— 每個設計決定都要有證據，而且評估必須容許推翻原本的假設。

三個分頁對應三層驗證：② 排序這步的估計有沒有校準（回測）、① 讀信這步
規則層跟 LLM 層各自的表現（解析）、④ 物料智能檢索抓不抓得到（檢索）。
沒贏的地方照實寫，不為了好看調參數（CLAUDE.md「不可違反的原則」第 4 條）。

原本跟「效益估算」合在同一個「評估實驗」暫時頁（Task 3），拆開的理由：
「這裡的方法準不準」和「這樣做值不值得」是兩個不同的問題，面試官問
「怎麼知道排序準」時不該還要滑過效益公式才找得到答案。
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

st.subheader("方法驗證")
st.caption("每個設計決定都要有證據，而且評估必須容許推翻原本的假設；沒贏的地方照實寫。")

tab_backtest, tab_parse, tab_rag = st.tabs(
    ["回測：到料估計與排序", "解析：規則層 vs LLM 層", "檢索：六種配置比較"])
for container, fname, cmd in (
    (tab_backtest, "回測結果.md", "py src/backtest.py"),
    (tab_parse, "實驗結果.md", "py src/evaluate.py"),
    (tab_rag, "RAG檢索評估.md", "py src/rag/evaluate_rag.py"),
):
    with container:
        path = ROOT / "output" / fname
        if path.exists():
            st.markdown(path.read_text(encoding="utf-8"))
        else:
            st.info(f"尚未執行評估。請於終端機執行： `{cmd}`")

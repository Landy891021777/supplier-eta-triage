# -*- coding: utf-8 -*-
"""
⚖️ 供應商歷史 —— 這家供應商說定日期之後，過去實際還會晚幾天。

行動清單上的「保守到料日」就是從這裡來的：只用「曾改期過的單」估計，
因為收到的通知本來就是已經跳票的那些。
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

ctx = ui_state.context()

st.subheader("供應商歷史表現")
st.caption(
    "行動清單上的「保守到料日」來自這裡：這家供應商說定日期之後，"
    "過去實際還會晚幾天。只用「曾改期過的單」估計，因為你收到的都是已經跳票的通知。")
st.warning(
    "⚠️ **本頁使用模擬歷史資料。** 歷史結果由 `src/generate_history.py` 的因果模型產生，"
    "非真實資料；在真實環境，輸入應該是 ERP 的收貨紀錄。")
try:
    pcol, rcol = st.columns([3, 2])
    with pcol:
        st.markdown("**各供應商歷史表現**")
        st.dataframe(ui_state.supplier_performance(), width="stretch",
                     hide_index=True, height=300)
    with rcol:
        st.markdown("**改期次數 vs 最終是否延遲**")
        st.dataframe(ui_state.reschedule_reliability(), width="stretch",
                     hide_index=True)
        st.caption(
            "改期越多次的單，最終仍延遲的比例是否越高？這是保守到料日"
            "只用「改期過的單」的依據。若資料顯示無關，估計就不該把改期單獨立出來。")
except (FileNotFoundError, RuntimeError) as e:
    st.info(f"{e}\n\n請先執行： `py src/generate_history.py`")

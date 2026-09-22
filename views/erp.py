# -*- coding: utf-8 -*-
"""
📄 ERP 單據 —— 一張採購單在 ERP 裡不是一列資料，是散落在六張表裡的一串單據。

最能說明「為什麼整合 ERP 不是撈一張表就好」的一頁。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import ui_state  # noqa: E402
import pipeline  # noqa: E402

ctx = ui_state.context()
cfg = ctx["cfg"]

st.subheader("ERP 單據軌跡")
st.caption(
    "一張採購單在 ERP 裡不是一列資料，是散落在六張表裡的一串單據。"
    "這一頁把它們串起來 — 也是最能說明「為什麼整合 ERP 不是撈一張表就好」的一頁。")
src6 = pipeline.get_data_source(cfg)
if not hasattr(src6, "document_trail"):
    st.info("目前的資料來源是 CSV，沒有單據結構。"
            "請將 config.yaml 的 data_source 改為 sqlite。")
else:
    c1, c2 = st.columns([1, 2])
    kind = c1.radio("單據狀態", ["在途（未收貨）", "已結案（有收貨）"],
                    help="已結案的單有實際到料日，是供應商歷史統計、"
                         "保守到料日估計與時間切分回測的資料來源。")
    pool = (src6.open_po_numbers() if kind.startswith("在途")
            else src6.closed_po_numbers())
    if not pool:
        st.info("這個狀態下沒有單據。")
    else:
        default = ("PO-2026-04205" if "PO-2026-04205" in pool else pool[0])
        po_sel = c2.selectbox("選擇採購單", pool, index=pool.index(default))
        trail = src6.document_trail(po_sel)
        for label, tdf in trail.items():
            st.markdown(f"**{label}**")
            if tdf.empty:
                st.caption("（無資料）")
            else:
                st.dataframe(tdf, width="stretch", hide_index=True)
        st.caption(
            "注意第 ④ 與第 ⑤ 張表：**承諾日不在採購單頭，改期次數也沒有現成欄位**。"
            "工具必須自己 JOIN 與 COUNT — 這就是接 ERP 真正的工作量所在。")

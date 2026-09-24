# -*- coding: utf-8 -*-
"""
📨 信件與解析軌跡 —— 原「原始信件」與「解析軌跡」兩個分頁合併。

上半部是解析軌跡總表與三個指標；下半部選一封信看全文，同時顯示
這封信對應的那一列解析軌跡——不必切分頁來回對照。
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
result, cfg, provider = ctx["result"], ctx["cfg"], ctx["provider"]

TRACE_RENAME = {
    "email_id": "信件", "rule_confidence": "規則層信心",
    "escalated": "是否升級", "llm_ok": "LLM成功",
    "llm_latency_ms": "LLM耗時(ms)", "llm_error": "錯誤",
    "final_layer": "最終採用", "reason": "升級判斷",
}
# pipeline.extract_one() 存的是內部代碼（rule／llm／rule(fallback)），
# 只給工程師看得懂；畫面上換成中文，不改資料本身。
FINAL_LAYER_LABEL_ZH = {"rule": "規則層", "llm": "LLM", "rule(fallback)": "規則層（LLM 失敗退回）"}

# ==================== 上半部：解析軌跡 ====================
st.subheader("解析軌跡（可稽核性）")
st.caption(
    "每封信走過哪幾層、為什麼升級、LLM 是否成功、花了多久。"
    "沒有軌跡的 AI 工具無法被稽核，出錯時也無從追查 — 物料企劃不會信任它。")
tr = result["traces"]
m1, m2, m3 = st.columns(3)
m1.metric("規則層即可處理", int((~tr["escalated"]).sum()))
m2.metric("升級至 LLM", int(tr["escalated"].sum()))
m3.metric("LLM 呼叫成功", int(tr["llm_ok"].sum()))
if not provider.available:
    st.warning(
        "目前無 LLM 金鑰，因此所有被升級的信件都退回規則層結果，"
        "並在行動清單中標示為需人工確認。這是刻意設計的降級行為："
        "工具寧可承認自己看不懂，也不能猜一個日期給物料企劃。")
src = pipeline.get_data_source(cfg)
if hasattr(src, "table_counts"):
    with st.expander("📚 資料來源：模擬 ERP 資料表結構（點開看）"):
        st.markdown(
            "工具讀的不是一張扁平表，而是一組結構貼近 SAP MM 的關聯式資料表。"
            "**承諾日、改期次數、有無二源這些欄位在 ERP 裡都不是現成的一欄**，"
            "必須用 SQL JOIN 推導出來 —— 這才是接 ERP 時真正要面對的工作。")
        counts = src.table_counts()
        sap = {
            "vendor_master": "供應商主檔 ≈ LFA1",
            "material_master": "物料主檔 ≈ MARA/MARC",
            "material_alternate": "替代料關係",
            "source_list": "來源清單 ≈ EORD（有無二源的來源）",
            "purchase_req": "請購單 ≈ EBAN（需求日的來源）",
            "po_header": "採購單頭 ≈ EKKO",
            "po_item": "採購單項次 ≈ EKPO",
            "po_schedule": "交貨排程行 ≈ EKET（承諾日的來源）",
            "po_change_log": "變更文件 ≈ CDHDR/CDPOS（改期次數的來源）",
            "goods_receipt": "收貨紀錄 ≈ MKPF/MSEG（保守到料日估計與回測的 outcome）",
        }
        st.dataframe(
            pd.DataFrame([{"資料表": t, "筆數": c, "對應（SAP 為例）": sap.get(t, "")}
                          for t, c in counts.items()]),
            width="stretch", hide_index=True)
        st.caption(
            "`goods_receipt` 刻意為空：它代表工具上線後才會累積的真實結果，"
            "也是供應商歷史統計、保守到料日估計與時間切分回測的唯一來源。"
            "表名與結構是參考公開資料整理的近似版本，非任何公司的真實 schema。")

# 顯示用的副本：最終採用欄轉中文、布林欄轉是／否，tr 本身（上面三個
# 指標、後面篩選某封信的那一列都要用布林值判斷）保持原始型別不變。
tr_display = tr.assign(
    final_layer=tr["final_layer"].map(FINAL_LAYER_LABEL_ZH).fillna(tr["final_layer"]),
    escalated=tr["escalated"].map({True: "是", False: "否"}),
    llm_ok=tr["llm_ok"].map({True: "是", False: "否"}))

st.dataframe(
    tr_display.rename(columns=TRACE_RENAME),
    width="stretch", hide_index=True, height=420)

# ==================== 下半部：原始信件 ====================
st.divider()
st.subheader("原始信件")
st.caption("含 10 封手寫的刁鑽案例（轉寄串、一信多單、模糊措辭、分批交貨…）。"
           "選一封信看全文，下方會一併顯示這封信的解析軌跡。")
emails = pipeline.load_emails()
opts = {f"{e['email_id']}｜{e['subject'][:50]}": e for e in emails}
chosen = st.selectbox("選擇信件", list(opts.keys()))
e = opts[chosen]
st.text(f"From: {e['supplier_id']}\nDate: {e['received_at']}\n"
        f"Subject: {e['subject']}\nTags: {', '.join(e.get('tags', []))}\n")
st.code(e["body"], language=None)

trace_row = tr_display[tr_display["email_id"] == e["email_id"]]
if not trace_row.empty:
    st.markdown("**這封信的解析軌跡**")
    st.dataframe(trace_row.rename(columns=TRACE_RENAME),
                width="stretch", hide_index=True)

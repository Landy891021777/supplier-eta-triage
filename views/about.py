# -*- coding: utf-8 -*-
"""
📖 專案簡介與導覽 —— 專案說明區的第一頁，給還沒看過這個工具的人用。

企劃工作區六頁是做給物料企劃每天用的；「專案說明」這三頁（本頁、
方法驗證、效益估算）是給看這份作品集的人用的，說明「這是什麼」
「怎麼驗證」「值多少」。本頁只負責第一件事：三分鐘看懂工具在做
什麼、從哪裡開始逛——所以放在「專案說明」第一頁。
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
stats = ctx["stats"]

st.subheader("專案簡介")

st.warning(
    "**誠實聲明**：本專案全部使用合成資料，沒有任何真實公司的資料，"
    "也沒有串接真實 ERP。真實的採購單與交期屬營業秘密，因此以程式"
    "產生資料，並把領域假設寫成可逐條檢視的程式碼。合成資料上的"
    "所有數字只能證明程式邏輯自洽，不能證明真實環境有效。"
)

st.markdown(
    "晶圓廠物料企劃每天要看供應商的交期回覆信：格式不一、中英夾雜、"
    "措辭模糊。這個工具讀懂這些信、對回採購單，算出每張單的"
    "**預估缺料天數**與**可投產日**，排出今天該先追哪幾張、附上理由"
    "和建議動作。"
)

# 唯一允許出現在本頁的數字：即時從 ui_state.context() 取，不寫死。
st.caption(
    f"目前這批合成資料：今日行動清單共 **{stats['actionable']}** 張，"
    f"其中 🔴 P1 **{stats['p1']}** 張。"
)

st.markdown("#### 三步導覽")

st.markdown("① 到「今日行動清單」看 P1 第一張，展開看為什麼排第一、建議動作是什麼。")
st.page_link("views/actions.py", label="今日行動清單", icon="📋")

st.markdown("② 到「供應商績效」看那家供應商過去說定日期後，通常還晚幾天——保守到料日就是從這裡來的。")
st.page_link("views/suppliers.py", label="供應商績效", icon="🏭")

st.markdown("③ 到「收貨處理天數」把那顆料調多幾天，回清單看排序怎麼變。")
st.page_link("views/receiving.py", label="收貨處理天數", icon="🛠")

st.markdown("#### 四個模組")
st.markdown(
    "| 模組 | 做什麼 | 對應頁面 |\n"
    "|---|---|---|\n"
    "| ① 讀信 | 把供應商回覆信轉成結構化的交期變更，判讀承諾強度"
    "（已確認／暫估／僅意向） | 📨 信件與解析軌跡 |\n"
    "| ② 排序 | 對回採購單，依十條領域規則排出今天該先追的單，"
    "非已確認的交期不寫回系統 | 📋 今日行動清單 |\n"
    "| ③ ERP 與歷史 | 模擬 ERP 算供應商準交率、保守到料日 "
    "| 🏭 供應商績效、📄 ERP 單據 |\n"
    "| ④ 物料智能檢索 | 用自然語言查採購單、料號、供應商，"
    "每個回答附出處 | 🔎 物料智能檢索 |\n"
)

st.markdown("#### 設計原則")
st.markdown(
    "- **排序可驗算**：排序鍵是預估缺料天數，兩個日期相減得到，"
    "不是一個模型吐出來的分數，企劃可以自己重算一次驗證。\n"
    "- **工具永不寫回 ERP、永不寄信**：企劃在畫面上登錄的確認交期"
    "只存在工具自己的資料庫，要更新交貨排程行仍要依公司流程操作。\n"
    "- **評估容許推翻原本的假設**：例如檢索的混合設計曾被評估結果"
    "推翻（見下方「方法驗證」），這種結果照實記錄，不為了好看調參數。"
)
st.page_link("views/validation.py", label="方法驗證", icon="🧪")

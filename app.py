# -*- coding: utf-8 -*-
"""
供應商交期回覆解析與影響評估工具 — Streamlit 介面（導覽外殼）。

介面設計的三個原則（給物料企劃用，不是給工程師用）：

1. **先給答案，再給過程。**
   打開就是「今天要處理的前幾件事」，不是一堆圖表。
   同仁早上只有十分鐘，工具必須在十秒內講完重點。

2. **每個判斷都能追問「為什麼」。**
   每一筆都可以展開看到預估缺料天數怎麼算出來、依據哪些事實旗標分級。
   說不出理由的排序，同仁用兩週就會棄用。

3. **不確定的地方要顯眼，不要藏。**
   承諾強度不是「已確認」的案件會被明確標示，且不覆寫系統承諾日。
   工具寧可讓人多看一眼，也不能讓人誤以為事情定了。

===========================  為什麼拆成 views/  ===========================
本檔只剩頁面設定、側邊欄、導覽分組——實際內容都在 `views/*.py`。
拆檔的理由：審查一頁改動時不必在一個六百行的檔案裡找位置；也讓
`AppTest` 可以逐頁個別開啟測試，某一頁忘記 import 什麼東西，測試
會精準點名是哪一頁壞了，而不是整支 app.py 一起報錯（見
tests/test_app_pages.py）。

介面分兩區：「企劃工作區」放物料企劃每天會用的頁面，「專案說明」放
給面試官看的專案簡介、方法驗證與效益估算（Plan 2 決策）。
=========================================================================
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import ui_state  # noqa: E402

st.set_page_config(page_title="Supply Chain AI Tool: Delivery Risk Prioritization",
                   page_icon="📦", layout="wide")

# 側邊欄是所有頁面共用的外殼，只在這裡呼叫一次；LLM 開關的值寫進
# st.session_state["use_llm"]，各頁的 ui_state.context() 再從那裡讀。
ui_state.sidebar()

pg = st.navigation({
    "企劃工作區": [
        st.Page("views/actions.py", title="今日行動清單", icon="📋", default=True),
        st.Page("views/search.py", title="物料智能檢索", icon="🔎"),
        st.Page("views/suppliers.py", title="供應商績效", icon="🏭"),
        st.Page("views/receiving.py", title="收貨處理天數", icon="🛠"),
        st.Page("views/erp.py", title="ERP 單據", icon="📄"),
        st.Page("views/emails.py", title="信件與解析軌跡", icon="📨"),
    ],
    "專案說明": [
        st.Page("views/about.py", title="專案簡介與導覽", icon="📖"),
        st.Page("views/validation.py", title="方法驗證", icon="🧪"),
        st.Page("views/benefit.py", title="效益估算", icon="📊"),
    ],
})
pg.run()

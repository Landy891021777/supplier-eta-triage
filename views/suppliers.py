# -*- coding: utf-8 -*-
"""
🏭 供應商績效 —— 這家供應商說定日期之後，過去實際還會晚幾天。

行動清單上的「保守到料日」就是從這裡來的：只用「曾改期過的單」估計，
因為收到的通知本來就是已經跳票的那些。下半部另外依承諾月份統計
每家供應商的交期表現，給企劃開會或評核用。
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
import exports  # noqa: E402

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
        # supplier_stats.supplier_performance() 回傳的是 supplier_id（代號）
        # 跟布林欄位，給企劃看的畫面要換成名字與中文是／否——這只是顯示
        # 轉換，不動 supplier_stats.py 的回傳格式（其他呼叫端可能要的
        # 就是原始代號與布林值）。
        perf = ui_state.supplier_performance().copy()
        names = ui_state.supplier_name_map()
        perf.insert(0, "供應商名稱", perf["supplier_id"].map(names))
        perf["樣本是否足夠"] = perf["樣本是否足夠"].map({True: "足夠", False: "不足"})
        st.dataframe(
            perf.rename(columns={"supplier_id": "供應商"}),
            width="stretch", hide_index=True, height=300)
    with rcol:
        st.markdown("**改期次數 vs 最終是否延遲**")
        st.dataframe(ui_state.reschedule_reliability(), width="stretch",
                     hide_index=True)
        st.caption(
            "改期越多次的單，最終仍延遲的比例是否越高？這是保守到料日"
            "只用「改期過的單」的依據。若資料顯示無關，估計就不該把改期單獨立出來。")
except (FileNotFoundError, RuntimeError) as e:
    st.info(f"{e}\n\n請先執行： `py src/generate_history.py`")

# ==================== 供應商月度績效 ====================
st.divider()
st.subheader("供應商月度績效")
st.caption("供應商評核的交期面：依承諾月份統計。品質與配合度沒有資料，這裡不做。")
st.caption(
    "已過承諾日仍未收貨的單算作延遲，延遲天數以到基準日為止計（低估——"
    "實際到貨只會更晚，不會更早）；「P80 延遲(天)」若是負數，代表這個月"
    "八成的單其實是提前到，不是延遲。")
try:
    outcomes = ui_state.outcomes_df()
except (FileNotFoundError, RuntimeError) as e:
    st.info(f"{e}\n\n請先執行： `py src/generate_history.py`")
else:
    # 在途逾期單（承諾日已過、還沒收貨）一併算進來，不然近期月份的準交率
    # 會被系統性高估——見 exports.supplier_monthly 檔頭的說明。
    monthly = exports.supplier_monthly(
        outcomes, open_pos=ui_state.open_pos_df(), as_of=str(ctx["result"]["as_of"]))
    if monthly.empty:
        st.info("沒有歷史資料可統計。")
    else:
        months = sorted(monthly["承諾月份"].unique())
        sel_month = st.selectbox("月份", months, index=len(months) - 1)
        month_view = monthly[monthly["承諾月份"] == sel_month].drop(columns=["承諾月份"])

        # 顯示用的副本：天數欄位的 NaN（樣本不足）換成「—」，比空白格更
        # 明確地告訴企劃「這裡沒有數字」而不是「忘記填」；準交率／改期比例
        # 維持原始浮點數，交給 column_config 的 NumberColumn 用百分比格式
        # 顯示（0.714286 → 71.4%），NaN 則由 Streamlit 原生顯示為空白。
        display = month_view.copy()
        for col in ("延遲時中位數(天)", "P80 延遲(天)"):
            if col in display.columns:
                display[col] = display[col].apply(lambda v: "—" if pd.isna(v) else v)
        st.dataframe(
            display, width="stretch", hide_index=True,
            column_config={
                "準交率": st.column_config.NumberColumn("準交率", format="percent"),
                "改期比例": st.column_config.NumberColumn("改期比例", format="percent"),
            })
        st.caption("「樣本不足」的供應商只顯示筆數，不給比例——一個月只有幾筆單時，"
                  "比例會因為一兩筆單大幅跳動，看似精確反而容易誤導評核。")
        st.download_button(
            "⬇️ 匯出供應商月度績效（Excel）",
            exports.monthly_workbook(monthly),
            file_name="供應商月度績效.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
